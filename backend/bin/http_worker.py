#!/usr/bin/env python
import os

def logger():
    pid = os.getpid()
    def prefix(m):
        print('[https-worker:%d] %s' % (pid, m))
    return prefix

log = logger()
log('started')

import json
import signal
import socket
import sys

# WARN signalling in this way is a race condition.
def SigRespond(SIG, FRM):
    log("received sig: %s from %s" % (FRM, SIG))

signal.signal(signal.SIGUSR1, SigRespond)

from twisted.internet import reactor, ssl, protocol, defer
from twisted.protocols import tls
from twisted.web.proxy import ReverseProxyResource
from twisted.web.server import Site

# When this executable is not within the systems standard path, the globaleaks
# module must add it to sys path manually. Hence the following line.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from globaleaks.utils.process import set_pdeathsig
from globaleaks.utils.sock import listen_tls_on_sock
from globaleaks.utils.ssl import TLSContextFactory


def SigQUIT(SIG, FRM):
    log('Quitting')
    try:
        reactor.stop()
    except Exception:
        pass

signal.signal(signal.SIGTERM, SigQUIT)
signal.signal(signal.SIGINT, SigQUIT)

set_pdeathsig(signal.SIGINT)

def config_wait(file_desc):
    log("listening for cfg on %d" % file_desc)
    f = os.fdopen(file_desc, 'r')
    s = f.read()
    f.close()
    cfg = json.loads(s)
    log("read config")
    return cfg

def setup_tls_proxy(cfg):
    resource = ReverseProxyResource(cfg['proxy_ip'], cfg['proxy_port'], '')
    http_factory = Site(resource)

    socket_fd = cfg['tls_socket_fd']

    log("Opening socket: %d : %s" % (socket_fd, os.fstat(socket_fd)))

    tls_factory = TLSContextFactory(cfg['key'],
                                    cfg['cert'],
                                    cfg['ssl_intermediate'],
                                    cfg['ssl_dh'],
                                    cfg['ssl_cipher_list'])

    port = listen_tls_on_sock(reactor, fd=socket_fd, factory=http_factory,
                              contextFactory=tls_factory)
    log("TLS proxy listening on %s" % port)

if __name__ == '__main__':
    try:
        cfg = config_wait(42)

        setup_tls_proxy(cfg)
    except Exception as e:
        log("setup failed with %s" % e)
        raise e

    reactor.run()
