import socket
import time
import logging
import picamera
import picamera.array
import re
import threading
from threading import Condition
import socketserver
import io
import numpy as np
from http import server
import requests

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
                self.frame = self.buffer.getvalue()[:]
            
            self.condition.notify_all()
            self.buffer.seek(0)

        return self.buffer.write(buf)

class DetectMotion(picamera.array.PiMotionAnalysis):
    def __init__(self, camera, magnitude, threshold, notifier):
        super(DetectMotion, self).__init__(camera, size=None)
        self.total_motion = 0.
        self.magnitude = magnitude
        self.threshold = threshold
        self.notifier = notifier

    def analyze(self, a):
        a = np.sqrt(
            np.square(a['x'].astype(np.float)) +
            np.square(a['y'].astype(np.float))
            ).clip(0, 255).astype(np.uint8)
        # If there're more than 10 vectors with a magnitude greater
        # than 60, then say we've detected motion

        self.total_motion = (a > self.magnitude).sum()

        # print('total_motion: {}'.format(self.total_motion))

        if self.total_motion > self.threshold:
            print('Motion detected: {}'.format(self.total_motion))
            if not self.notifier is None:
                self.notifier.notify()

class Notifier(object):
    def __init__(self, url, data=None):
        self.url = url
        self.data = data

        self.condition = Condition()
        self.completed = False
        self.thread = threading.Thread(target=self.notify_thread)
        self.thread.start()

    def notify_thread(self):
        self.condition.acquire()

        while not self.completed:
            self.condition.wait()

            if self.completed:
                break

            # no need to hold the lock while we make the request
            self.condition.release()
            try:
                print('sending notification')
                requests.post(self.url, data=self.data)
            except Exception as e:
                print('Exception when notifying: {}'.format(e))
            finally:
                self.condition.acquire()
        
        self.condition.release()

    def notify(self):
        with self.condition:
            self.condition.notify_all()
    
    def stop(self):
        with self.condition:
            self.completed = True
            self.condition.notify_all()

        self.thread.join()
                



class MotionOutput(object):
    def __init__(self):
        self.frame = None
        self.buffer = io.BytesIO()
        self.condition = Condition()

    def write(self, buf):
        self.buffer.seek(0)
        ret = self.buffer.write(buf)
        self.buffer.truncate()

        with self.condition:
            self.frame = self.buffer.getvalue()[:]

        self.condition.notify_all()

        return ret

class WebHandler(server.BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs):
        pass

    def do_GET(self):
        m = re.search('^([^\?]+)\??.*$', self.path)

        path = m.group(1)

        if path == '/':
            content = index_HTML.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', len(content))
            self.end_headers()
            self.wfile.write(content)
        elif path == '/video.jpg':
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
        # elif path == '/motion.bin':
        #     self.send_response(200)
        #     self.send_header('Age', 0)
        #     self.send_header('Cache-Control', 'no-cache, private')
        #     self.send_header('Pragma', 'no-cache')
        #     self.send_header('Content-Type', 'binary/octet-stream')

        #     try:
        #         with self.server.motionOutput.condition:
        #             # no need to wait, just get the frame
        #             # self.server.motionOutput.condition.wait()
        #             frame = self.server.motionOutput.frame
                
        #         self.send_header('Content-Length', len(frame))
        #         self.end_headers()

        #         self.wfile.write(frame)
        #     except Exception as e:
        #         logging.warning('Error getting motion %s', str(e))

        elif path == '/frame.jpg':
            self.send_response(200)
            self.send_header('Age', 0)
            self.send_header('Cache-Control', 'no-cache, private')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Content-Type', 'image/jpeg')

            try:
                with self.server.output.condition:
                    # no need to wait, just get the frame
                    # self.server.output.condition.wait()
                    frame = bytes(self.server.output.frame)
                
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
    # daemon_threads = True

    def __init__(self, outputs, *args, **kwargs):
        super(WebServer, self).__init__(*args, **kwargs)
        self.output = output
        # self.motionOutput = motionOutput

# Accept a single connection and make a file-like object out of it
# connection = server_socket.accept()[0].makefile('wb')

print('starting picamera')

camera = picamera.PiCamera(resolution=(1640,1248), framerate=24)
#camera.resolution = (1440, 1080)
# camera.resolution = (1640, 1248)
# camera.framerate = 24

print('creating socket server')
server = SocketServer('0.0.0.0', 8000)

print('creating mjpeg outputer')
output = StreamingOutput()

print('creating notifier')
notifier = Notifier(url='http://mirror.local:9080/', data='"on"')
# motionOutput = MotionOutput()
detectMotion = DetectMotion(camera, magnitude=60, threshold=10, notifier=notifier)

print('creating webserver')
webServer = WebServer(output, ('', 8888), WebHandler)
    

try:
    print('starting mjpeg recorder')
    camera.start_recording(output, format='mjpeg', splitter_port=2, resize=(640,480))

    print('starting h264 recorder')
    camera.start_recording(server, format='h264', level='4.2', profile='high', intra_refresh='cyclic', inline_headers=True, sps_timing=True, motion_output=detectMotion)
    webServer.serve_forever()
except KeyboardInterrupt:
    pass

camera.stop_recording()
camera.stop_recording(splitter_port=2)
server.close()
notifier.stop()
camera.close()
webServer.shutdown()

# camera.stop_recording()
# server.close()
