import errno
import socket

from django.contrib.staticfiles.management.commands.runserver import Command as StaticFilesRunserverCommand
from django.core.servers.basehttp import WSGIServer


class AutoPortWSGIServer(WSGIServer):
    unavailable_errors = {errno.EADDRINUSE, errno.EACCES}

    def server_bind(self):
        address, requested_port = self.server_address
        if not 0 < requested_port <= 65535:
            return super().server_bind()

        for port in range(requested_port, 65536):
            self.server_address = (address, port)
            try:
                super().server_bind()
            except OSError as error:
                if (
                    error.errno not in self.unavailable_errors
                    and getattr(error, "winerror", None) != 10013
                ) or port == 65535:
                    raise
                self.socket.close()
                self.socket = socket.socket(self.address_family, self.socket_type)
            else:
                self.requested_port = requested_port
                return


class Command(StaticFilesRunserverCommand):
    server_cls = AutoPortWSGIServer

    def on_bind(self, server_port):
        if server_port != int(self.port):
            self.stderr.write(
                self.style.WARNING(
                    f"Port {self.port} is unavailable; using port {server_port} instead."
                )
            )
        super().on_bind(server_port)
