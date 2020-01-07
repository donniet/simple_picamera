import socket
import time
import logging
import picamera
import threading
from threading import Condition
import socketserver
import io
from http import server

# server_socket = socket.socket()
# server_socket.bind(('0.0.0.0', 8000))
# server_socket.listen(0)

index_HTML = """\
<html>
    <head>
        <title>simple picamera</title>
    </head>
    <body>
        <h1>Hello!</h1>
    </body>
</html>
"""

class StreamingOutput(object):
    def __init__(self):
        self.frame = None
        self.buffer = io.BytesIO()
        self.condition = Condition()

    def write(self, buf):
        if buf.startswith(b'\xff\xd8'):
            # New frame, copy the existing buffer's content and notify all
            # clients it's available
            self.buffer.truncate()
            with self.condition:
                self.frame = self.buffer.getvalue()
                self.condition.notify_all()
            self.buffer.seek(0)
        return self.buffer.write(buf)

class WebHandler(server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            content = index_HTML.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', len(content))
            self.end_headers()
            self.wfile.write(content)
        elif self.path == '/stream.mjpg':
            self.send_response(200)
            self.send_header('Age', 0)
            self.send_header('Cache-Control', 'no-cache, private')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=FRAME')
            self.end_headers()
            try:
                while True:
                    with self.server.output.condition:
                        self.server.output.condition.wait()
                        frame = self.server.output.frame
                    self.wfile.write(b'--FRAME\r\n')
                    self.send_header('Content-Type', 'image/jpeg')
                    self.send_header('Content-Length', len(frame))
                    self.end_headers()
                    self.wfile.write(frame)
                    self.wfile.write(b'\r\n')
            except Exception as e:
                logging.warning(
                    'Removed streaming client %s: %s',
                    self.client_address, str(e))
        elif self.path == '/frame.jpg':
            self.send_response(200)
            self.send_header('Age', 0)
            self.send_header('Cache-Control', 'no-cache, private')
            self.send_header('Pragma', 'no-cache')

            try:
                with self.server.output.condition:
                    self.server.output.condition.wait()
                    frame = self.server.output.frame
                
                self.send_header('Content-Length', len(frame))
                self.end_headers()

                self.wfile.write(frame)
            except Exception as e:
                logging.warning('Error getting frame %s', str(e))

        else:
            self.send_error(404)
            self.end_headers()

class SocketServer(object):
    def __init__(self, addr, port):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((addr, port))
        self.sock.listen(0)
        self.connections = dict()
        self.lock = threading.Lock()

        self.accepter = threading.Thread(target=self._accepter)
        self.accepter.start()

    def _accepter(self):
        print('accepter started')
        while True:
            try:
                (conn, addr) = self.sock.accept()
            except OSError:
                break

            print('accepted: {}'.format(addr))

            self.lock.acquire()
            self.connections[addr] = conn.makefile('wb')
            self.lock.release()
        
        self.lock.acquire()
        for addr in self.connections:
            self.connections[addr].close()
        self.lock.release()

    def write(self, buf):
        self.lock.acquire()
        conns = self.connections.copy()
        self.lock.release()

        to_remove = []

        for addr in conns:
            c = conns[addr]
            try:
                c.write(buf)
            except BrokenPipeError:
                to_remove.append(addr)
            except ConnectionResetError:
                to_remove.append(addr)
            except ValueError:
                to_remove.append(addr)

        self.lock.acquire()
        for addr in to_remove:
            try:
                conns[addr].close()
            except BrokenPipeError:
                pass
            except ConnectionResetError:
                pass
            except ValueError:
                pass

            del conns[addr]
        self.lock.release()

        return len(buf)

    def close(self):
        self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()
        print('socket closed, joining accepter thread')
        self.accepter.join()
        
class WebServer(socketserver.ThreadingMixIn, server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, output, *args, **kwargs):
        super(WebServer, self).__init__(*args, **kwargs)
        self.output = output

# Accept a single connection and make a file-like object out of it
# connection = server_socket.accept()[0].makefile('wb')

camera = picamera.PiCamera()
camera.resolution = (1440, 1080)
camera.framerate = 24
server = SocketServer('0.0.0.0', 8000)
output = StreamingOutput()
webServer = WebServer(output, ('', 8080), WebHandler)
    

try:
    camera.start_recording(output, format='mjpeg', splitter_port=2, resize=(640,480))
    camera.start_recording(server, format='h264', level='4.2', profile='high')
    webServer.serve_forever()
except KeyboardInterrupt:
    camera.stop_recording()
    camera.stop_recording(splitter_port=2)
    server.close()
    webServer.shutdown()

# camera.stop_recording()
# server.close()