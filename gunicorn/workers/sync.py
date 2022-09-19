# -*- coding: utf-8 -
#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.
#

from datetime import datetime
import errno
import os
import select
import socket
import ssl
import sys

import gunicorn.http as http
import gunicorn.http.wsgi as wsgi
import gunicorn.util as util
import gunicorn.workers.base as base
from gunicorn.http import Request
import logging

log = logging.getLogger(__name__)


class StopWaiting(Exception):
    """ exception raised to stop waiting for a connection """


class SyncWorker(base.Worker):

    def accept(self, listener):
        client, addr = listener.accept()
        self.log.info(f"[SyncWorker accept] {client=} {addr=}")
        # 套接字client 设置阻塞状态
        client.setblocking(1)
        """
        阻塞模式下，如果接收缓冲区没有数据可读，recv() 会一直阻塞，直到有数据可读。
        
        非阻塞模式下，recv() 从接收缓冲区读取数据时，如果没有数据，也会立马返回，并抛出 BlockingIOError: [Errno 11] Resource temporarily unavailable 异常。
        """
        util.close_on_exec(client)
        # 从套接字接受数据，调用处理函数，并关闭client
        self.handle(listener, client, addr)

    def wait(self, timeout):
        try:
            self.notify()
            ret = select.select(self.wait_fds, [], [], timeout)
            if ret[0]:
                if self.PIPE[0] in ret[0]:
                    os.read(self.PIPE[0], 1)
                return ret[0]

        except select.error as e:
            if e.args[0] == errno.EINTR:
                return self.sockets
            if e.args[0] == errno.EBADF:
                if self.nr < 0:
                    return self.sockets
                else:
                    raise StopWaiting
            raise

    def is_parent_alive(self):
        # If our parent changed then we shut down.
        if self.ppid != os.getppid():
            self.log.info("Parent changed, shutting down: %s", self)
            return False
        return True

    def run_for_one(self, timeout):
        listener = self.sockets[0]
        while self.alive:
            # worker进程记录存活时间
            self.notify()

            # Accept a connection. If we get an error telling us
            # that no connection is waiting we fall down to the
            # select which is where we'll wait for a bit for new
            # workers to come give us some love.
            try:
                # 建立新连接，接收数据
                self.accept(listener)
                # Keep processing clients until no one is waiting. This
                # prevents the need to select() for every client that we
                # process.
                continue

            except EnvironmentError as e:
                log.warning(
                    f"[SyncWorker run_for_one] while accept {e}"
                )
                if e.errno not in (errno.EAGAIN, errno.ECONNABORTED,
                                   errno.EWOULDBLOCK):
                    raise

            if not self.is_parent_alive():
                return

            try:
                self.log.info(
                    f"[SyncWorker run_for_one] wait"
                )
                self.wait(timeout)
            except StopWaiting:
                return

    def run_for_multiple(self, timeout):
        while self.alive:
            self.notify()

            try:
                ready = self.wait(timeout)
            except StopWaiting:
                return

            if ready is not None:
                for listener in ready:
                    if listener == self.PIPE[0]:
                        continue

                    try:
                        self.accept(listener)
                    except EnvironmentError as e:
                        if e.errno not in (errno.EAGAIN, errno.ECONNABORTED,
                                           errno.EWOULDBLOCK):
                            raise

            if not self.is_parent_alive():
                return

    def run(self):
        # if no timeout is given the worker will never wait and will
        # use the CPU for nothing. This minimal timeout prevent it.
        timeout = self.timeout or 0.5

        # self.socket appears to lose its blocking status after
        # we fork in the arbiter. Reset it here.
        # 从master进程fork出来的worker进程继承的套接字失去不阻塞状态，在这里重置不阻塞状态
        for s in self.sockets:
            # 套接字不阻塞
            s.setblocking(False)

        if len(self.sockets) > 1:
            self.run_for_multiple(timeout)
        else:
            self.run_for_one(timeout)

    def handle(self, listener, client, addr):
        from gunicorn.http.message import Request

        self.log.info(f"[SyncWorker accept] {listener=} {client=} {addr=}")
        req = None
        try:
            if self.cfg.is_ssl:
                client = ssl.wrap_socket(client, server_side=True,
                                         **self.cfg.ssl_options)
            parser = http.RequestParser(self.cfg, client, addr)
            req: Request = next(parser)
            self.log.info(f"[SyncWorker parser] {listener=} {req} {client=} {addr=}")
            self.handle_request(listener, req, client, addr)
        except http.errors.NoMoreData as e:
            self.log.debug("Ignored premature client disconnection. %s", e)
        except StopIteration as e:
            self.log.debug("Closing connection. %s", e)
        except ssl.SSLError as e:
            if e.args[0] == ssl.SSL_ERROR_EOF:
                self.log.debug("ssl connection closed")
                client.close()
            else:
                self.log.debug("Error processing SSL request.")
                self.handle_error(req, client, addr, e)
        except EnvironmentError as e:
            if e.errno not in (errno.EPIPE, errno.ECONNRESET, errno.ENOTCONN):
                self.log.exception("Socket error processing request.")
            else:
                if e.errno == errno.ECONNRESET:
                    self.log.debug("Ignoring connection reset")
                elif e.errno == errno.ENOTCONN:
                    self.log.debug("Ignoring socket not connected")
                else:
                    self.log.debug("Ignoring EPIPE")
        except Exception as e:
            self.handle_error(req, client, addr, e)
        finally:
            util.close(client)

    def handle_request(self, listener, req: Request, client, addr):
        self.log.info(
            f"[SyncWorker handle_request] {listener=} {req=} {client=} {addr=}"
        )
        environ = {}
        resp = None
        try:
            # pre_request 自定义操作
            self.cfg.pre_request(self, req)
            request_start = datetime.now()
            resp, environ = wsgi.create(req, client, addr,
                                        listener.getsockname(), self.cfg)
            # Force the connection closed until someone shows
            # a buffering proxy that supports Keep-Alive to
            # the backend.
            resp.force_close()
            self.nr += 1
            if self.nr >= self.max_requests:
                self.log.info("Autorestarting worker after current request.")
                self.alive = False
            respiter = self.wsgi(environ, resp.start_response)
            try:
                if isinstance(respiter, environ['wsgi.file_wrapper']):
                    resp.write_file(respiter)
                else:
                    for item in respiter:
                        resp.write(item)
                resp.close()
                request_time = datetime.now() - request_start
                self.log.access(resp, req, environ, request_time)
            finally:
                if hasattr(respiter, "close"):
                    respiter.close()
        except EnvironmentError:
            # pass to next try-except level
            util.reraise(*sys.exc_info())
        except Exception:
            if resp and resp.headers_sent:
                # If the requests have already been sent, we should close the
                # connection to indicate the error.
                self.log.exception("Error handling request")
                try:
                    client.shutdown(socket.SHUT_RDWR)
                    client.close()
                except EnvironmentError:
                    pass
                raise StopIteration()
            raise
        finally:
            try:
                self.cfg.post_request(self, req, environ, resp)
            except Exception:
                self.log.exception("Exception in post_request hook")
