# -*- encoding: utf-8 -*-
#
#   mailflush_sched
#   ***************
#
# Flush the email that has to be sent, is based on EventLog
# database table.

from cyclone.util import ObjectDict as OD
from storm.expr import Desc
from twisted.internet.defer import inlineCallbacks, Deferred, returnValue

from globaleaks.models import EventLogs, Notification
from globaleaks.handlers.admin import db_admin_serialize_node
from globaleaks.handlers.admin.notification import admin_serialize_notification
from globaleaks.jobs.base import GLJob
from globaleaks.settings import transact, transact_ro, GLSetting
from globaleaks.plugins import notification
from globaleaks.utils.mailutils import MIME_mail_build, sendmail
from globaleaks.utils.utility import deferred_sleep, log
from globaleaks.utils.templating import Templating

class NotificationMail:

    def __init__(self, plugin_used):
        self.plugin_used = plugin_used

    @inlineCallbacks
    def do_every_notification(self, eventOD):

        notify = self.plugin_used.do_notify(eventOD)

        if isinstance(notify, Deferred):
            notify.addCallback(self.every_notification_succeeded, eventOD.storm_id)
            notify.addErrback(self.every_notification_failed, eventOD.storm_id)
            yield notify
        else:
            yield self.every_notification_failed(None, eventOD.storm_id)

    @transact
    def every_notification_succeeded(self, store, result, event_id):
        if event_id:
            log.debug("Mail delivered correctly for event %s, [%s]" % (event_id, result))
            evnt = store.find(EventLogs, EventLogs.id == event_id).one()
            evnt.mail_sent = True
        else:
            log.debug("Mail digest correctly sent")

    @transact
    def every_notification_failed(self, store, failure, event_id):
        if event_id:
            log.err("Mail delivery failure for event %s (%s)" % (event_id, failure))
            evnt = store.find(EventLogs, EventLogs.id == event_id).one()
            evnt.mail_sent = True
        else:
            log.err("Mail error error")

@transact
def mark_event_as_notified_in_digest(store, evnt):
    evnt = store.find(EventLogs, EventLogs.id == evnt.storm_id).one()
    evnt.mail_sent = True


@transact_ro
def load_complete_events(store, event_number=GLSetting.notification_limit):
    """
    _complete_ is explicit because do not serialize, but make an OD() of the description.

    event_number represent the amount of event that can be returned by the function,
    event to be notified are taken in account later.
    """

    node_desc = db_admin_serialize_node(store, GLSetting.defaults.language)

    event_list = []
    storedevnts = store.find(EventLogs, EventLogs.mail_sent == False)
    storedevnts.order_by(Desc(EventLogs.creation_date))

    debug_event_counter = {}
    for i, stev in enumerate(storedevnts):

        if len(event_list) == event_number:
            log.debug("Maximum number of notification event reach (Mailflush) %d, after %d" %
                      (event_number, i ))
            break

        debug_event_counter.setdefault(stev.event_reference['kind'], 0)
        debug_event_counter[stev.event_reference['kind']] += 1

        if not stev.description['receiver_info']['file_notification'] and \
                        stev.event_reference['kind'] == 'File':
            continue
        if not stev.description['receiver_info']['message_notification'] and \
                        stev.event_reference['kind'] == 'Message':
            continue
        if not stev.description['receiver_info']['comment_notification'] and \
                        stev.event_reference['kind'] == 'Comment':
            continue
        if not stev.description['receiver_info']['tip_notification'] and \
                (stev.event_reference['kind'] == 'Tip' or
                          stev.event_reference['kind'] == 'UpcomingExpireTip'):
            continue

        eventcomplete = OD()

        # node level information are not stored in the node, but fetch now
        eventcomplete.notification_settings = admin_serialize_notification(
            store.find(Notification).one(), stev.description['receiver_info']['language']
        )

        eventcomplete.node_info = node_desc

        # event level information are decoded form DB in the old 'Event'|nametuple format:
        eventcomplete.receiver_info = stev.description['receiver_info']
        eventcomplete.tip_info = stev.description['tip_info']
        eventcomplete.subevent_info = stev.description['subevent_info']
        eventcomplete.context_info = stev.description['context_info']
        eventcomplete.steps_info = stev.description['steps_info']

        eventcomplete.type = stev.description['type'] # 'Tip', 'Comment'
        eventcomplete.trigger = stev.event_reference['kind'] # 'plaintext_blah' ...

        eventcomplete.storm_id = stev.id

        event_list.append(eventcomplete)

    if debug_event_counter:
        log.debug("load_complete_events: %s" % debug_event_counter)

    return event_list


def look_for_digest_opportunities(queue_events):
    """
    Generate
    {
        UUIDv4 : {
                'events' : [0, 4, 5, ... ],
                'kinds': {
                        'Tip' : 1,
                        ...
                    }
    }
    so we can operate directly on the list and removing the list element we use
    """

    digest_dict = {}
    for pos, qe in enumerate(queue_events):
        digest_dict.setdefault(qe.receiver_info['username'], {
            'events': [],
            'kinds': {}
        })
        digest_dict[qe.receiver_info['username']]['events'].append(pos)
        digest_dict[qe.receiver_info['username']]['kinds'].setdefault(qe.type, 0)
        digest_dict[qe.receiver_info['username']]['kinds'][qe.type] += 1


    return digest_dict

class MailflushSchedule(GLJob):

    # sorry for the double negation, we are sleeping two seconds below.
    skip_sleep = False

    def ping_mail_flush(self, notification_settings, receivers_syntesis):
        """
        TODO This function should be implemented as a clean and testable plugin in the
        way defined in plugin/base.py and plugin/notification.py, and/or is the opportunity
        to review these classes, at the moment is a simplified version that just create a
        ping email and send it via sendmail.
        """

        for _, data in receivers_syntesis.iteritems():

            receiver_dict, winks = data

            receiver_name = receiver_dict['name']
            receiver_email = receiver_dict['ping_mail_address']

            fakeevent = OD()
            fakeevent.type = u'ping_mail'
            fakeevent.node_info = None
            fakeevent.context_info = None
            fakeevent.steps_info = None
            fakeevent.receiver_info = receiver_dict
            fakeevent.tip_info = None
            fakeevent.subevent_info = {'counter': winks}

            body = Templating().format_template(
                notification_settings['ping_mail_template'], fakeevent)
            title = Templating().format_template(
                notification_settings['ping_mail_title'], fakeevent)

            # so comfortable for a developer!! :)
            source_mail_name = GLSetting.developer_name if GLSetting.devel_mode \
                else GLSetting.memory_copy.notif_source_name
            message = MIME_mail_build(source_mail_name,
                                      GLSetting.memory_copy.notif_source_email,
                                      receiver_name,
                                      receiver_email,
                                      title,
                                      body)

            fakeevent2 = OD()
            fakeevent2.type = "Ping mail for %s (%d info)" % (receiver_email, winks)

            return sendmail(authentication_username=GLSetting.memory_copy.notif_username,
                            authentication_password=GLSetting.memory_copy.notif_password,
                            from_address= GLSetting.memory_copy.notif_source_email,
                            to_address= [receiver_email],
                            message_file=message,
                            smtp_host=GLSetting.memory_copy.notif_server,
                            smtp_port=GLSetting.memory_copy.notif_port,
                            security=GLSetting.memory_copy.notif_security,
                            event=fakeevent2)

    @inlineCallbacks
    def operation(self):
        if not GLSetting.memory_copy.receiver_notif_enable:
            log.debug("MailFlush: Receiver(s) Notification disabled by Admin")
            return

        queue_events = yield load_complete_events()

        if not len(queue_events):
            returnValue(None)

        plugin = getattr(notification, GLSetting.notification_plugins[0])()
        # This wrap calls plugin/notification.MailNotification
        notifcb = NotificationMail(plugin)

        # figure out if some notification are part of the same receiver,
        # so they can be in bulk mode, sending only one email
        receiver_bulks = look_for_digest_opportunities(queue_events)

        digest_used_event = [] # contain a list of integer, position of queue_events
        digest_produced_event = [] # contain a list of Event object

        digest_separator = '%s' % ("=" * 50)

        for _, digest_obj in receiver_bulks.iteritems():

            # Copy from the event of the receiver infos
            event_copy = None

            if len(digest_obj['events']) > 1:

                digest_body = ""
                for qe_index in digest_obj['events']:
                    body, title = plugin.get_mail_body_and_title(queue_events[qe_index])
                    digest_body = "%s%s\n%s\n%s\n\n%s\n\n" % (
                        digest_body,
                        title,
                        "%s" % ("+" * (len(title) -1) ),
                        body,
                        digest_separator
                    )
                    digest_used_event.append(qe_index)

                    if not event_copy:
                        event_copy = queue_events[qe_index]


                # create new digest event based on the new content
                nde = OD()

                nde.node_info = event_copy.node_info
                nde.notification_settings = event_copy.notification_settings
                nde.receiver_info = event_copy.receiver_info
                nde.tip_info={
                                 'body': digest_body,
                                 'title': nde.notification_settings['notification_digest_mail_title']
                             }
                nde.subevent_info = None
                nde.context_info = event_copy.context_info
                nde.steps_info = event_copy.steps_info
                nde.type = 'digest'
                nde.trigger = event_copy.trigger

                nde.storm_id = None

                digest_produced_event.append(nde)


        for digest_event in digest_produced_event:

            yield notifcb.do_every_notification(digest_event)

            if not self.skip_sleep:
                yield deferred_sleep(2)


        # this is the notification of the standard event, it ignores
        # all the event already managed by the digest
        for qe_pos, qe in enumerate(queue_events):
            if qe_pos in digest_used_event:
                yield mark_event_as_notified_in_digest(queue_events[qe_pos])
                continue

            yield notifcb.do_every_notification(qe)

            if not self.skip_sleep:
                yield deferred_sleep(2)


        # This is the notification of the ping, if configured
        receivers_synthesis = {}
        for qe in queue_events:

            if not qe.receiver_info['ping_notification']:
                continue

            if qe.receiver_info['id'] not in receivers_synthesis:
                receivers_synthesis[qe.receiver_info['id']] = [qe.receiver_info, 1]
            else:
                receivers_synthesis[qe.receiver_info['id']][1] += 1

        if len(receivers_synthesis.keys()):
            # I'm taking the element [0] of the list but every element has the same
            # notification setting. is passed to ping_mail_flush because of the Templating()
            yield self.ping_mail_flush(queue_events[0].notification_settings,
                                       receivers_synthesis)

        # Whishlist: implement digest as an appropriate plugin
