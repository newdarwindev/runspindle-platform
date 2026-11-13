"""CONNECT-only fleet egress: exact domain policy and public IPv4 on every dial.

No guest DNS, arbitrary ports, HTTP forwarding, credentials or private address
fallback. TLS remains end to end. DNS answers are never trusted across requests.
"""
import ipaddress
import json
import select
import re
import socket
import socketserver
import sys
import time


def destination(authority, policy, resolver=socket.getaddrinfo):
    host, separator, port = authority.rpartition(':')
    host = host.lower()
    if not separator or port != '443' or not host or host.endswith('.'):
        raise ValueError('Only canonical HTTPS authorities permitted')
    if not any(host == item or (item.startswith('*.') and host.endswith(item[1:])
                               and host != item[2:]) for item in policy):
        raise ValueError('Domain denied')
    answers = resolver(host, 443, socket.AF_INET, socket.SOCK_STREAM)
    addresses = sorted({answer[4][0] for answer in answers})
    if not addresses or any(not ipaddress.ip_address(a).is_global or ipaddress.ip_address(a).is_multicast or ipaddress.ip_address(a).is_reserved for a in addresses):
        raise ValueError('Private/reserved DNS answer denied')
    return addresses


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(15)
        remote = None
        target = ""
        try:
            first = self.rfile.readline(4097)
            if len(first) > 4096:
                raise ValueError('Request too long')
            method, target, version = first.decode('ascii').strip().split()
            if method != 'CONNECT' or version not in ('HTTP/1.0', 'HTTP/1.1'):
                raise ValueError('CONNECT required')
            size = 0
            while True:
                header = self.rfile.readline(4097)
                size += len(header)
                if size > 16384 or len(header) > 4096 or not header:
                    raise ValueError('Invalid headers')
                if header == b'\r\n':
                    break
            policy = self.server.policy
            if getattr(self.server, 'policy_path', None):
                with open(self.server.policy_path) as stream:
                    policy = json.load(stream)['https_domains']
            addresses = destination(target, policy)
            # Connect directly to a validated numeric IP; no second DNS lookup.
            for address in addresses:
                try:
                    remote = socket.create_connection((address, 443), timeout=15)
                    break
                except OSError:
                    continue
            if remote is None:
                raise OSError('No approved upstream available')
            self.wfile.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
            self.wfile.flush()
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                readable, _, _ = select.select([self.connection, remote], [], [], 30)
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    (remote if source is self.connection else self.connection).sendall(data)
        except (OSError, ValueError, UnicodeError) as error:
            hostname = target.rpartition(':')[0]
            if re.fullmatch(r'[a-zA-Z0-9.-]{1,253}', hostname):
                print(json.dumps({'decision': 'deny', 'hostname': hostname, 'reason': str(error)}), flush=True)
            try:
                self.wfile.write(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n')
            except OSError:
                pass
        finally:
            if remote:
                remote.close()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 128


def main():
    policy = json.load(open(sys.argv[1]))['https_domains']
    with Server((sys.argv[2], 3128), Handler) as server:
        server.policy = policy
        server.policy_path = sys.argv[1]
        server.serve_forever()


if __name__ == '__main__':
    main()
