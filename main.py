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
import time
import argparse
import queue
from datetime import datetime, timedelta
import dateutil.tz
import socket

from prometheus_client import Summary, Counter, Gauge, MetricsHandler, Info

ENABLE_STATS = True

TOTAL_MOTION = Summary('picamera_motion', 'sum of motion vectors above threshold')
JPEG_FRAME_SEND_TIME = Summary('picamera_jpeg_frame_send_seconds', 'time to send a JPEG frame')
JPEG_FRAME_TIME = Summary('picamera_jpeg_frame_seconds', 'time between frames')
CLIENTS = Summary('picamera_clients', 'number of connected clients')
JPEG_BYTES_SENT = Summary('picamera_jpeg_send_bytes', 'bytes of jpeg data sent')
H264_BYTES_SENT = Summary('picamera_h264_send_bytes', 'bytes of h264 data sent')
JPEG_CLIENTS = Gauge('picamera_jpeg_clients', 'number of concurrent jpeg clients')
INFO = Info('picamera', 'information about this picamera instance')




# server_socket = socket.socket()
# server_socket.bind(('0.0.0.0', 8000))
# server_socket.listen(0)

EPOCH = datetime(1970, 1, 1, 0, 0, 0, tzinfo=dateutil.tz.tzutc())

index_HTML = """\
<html>
    <head>
        <title>simple picamera</title>
    </head>
    <body>
        <h1>Hello!</h1>
        <ul>
            <li><a href="/metrics">Metrics</a></li>
            <li><a href="/video.jpg">MPEG Video</a></li>
            <li><a href="/frame.jpg">Single Frame</a></li>
    </body>
</html>
"""

class StreamingOutput(object):
    def __init__(self):
        self.frame = None
        self.buffer = io.BytesIO()
        self.condition = Condition()
        self.last = datetime.now()


    def write(self, buf):
        if buf.startswith(b'\xff\xd8'):
            # New frame, copy the existing buffer's content and notify all
            # clients it's available
            self.buffer.truncate()
            with self.condition:
                self.frame = self.buffer.getvalue()[:]
                self.condition.notify_all()
            self.buffer.seek(0)

            if ENABLE_STATS:
                t1 = datetime.now()
                JPEG_FRAME_TIME.observe((t1 - self.last).total_seconds())
                self.last = t1

        return self.buffer.write(buf)
        
class WebHandler(MetricsHandler):
    def log_message(self, *args, **kwargs):
        pass

    def do_GET(self):
        m = self.server.exp.search(self.path)

        path = m.group(1)

        if path == '/':
            content = index_HTML.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', len(content))
            self.end_headers()
            self.wfile.write(content)
        if path == '/metrics':
            return super(WebHandler, self).do_GET()
        elif path == '/video.jpg':
            if ENABLE_STATS:
                JPEG_CLIENTS.inc()
            self.send_response(200)
            self.send_header('Age', 0)
            self.send_header('Cache-Control', 'no-cache, private')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=FRAME')
            self.end_headers()
            try:
                while True:
                    t1 = datetime.now()

                    with self.server.output.condition:
                        frame = self.server.output.frame[:]
                    self.wfile.write(b'--FRAME\r\n')
                    self.send_header('Content-Type', 'image/jpeg')
                    self.send_header('Content-Length', len(frame))
                    self.end_headers()
                    self.wfile.write(frame)
                    self.wfile.write(b'\r\n')

                    time.sleep(1.0/12.)


                    if ENABLE_STATS:
                        JPEG_BYTES_SENT.observe(len(frame))
                        t2 = datetime.now()
                        JPEG_FRAME_SEND_TIME.observe((t2 - t1).total_seconds())

            except Exception as e:
                logging.warning(
                    'Removed streaming client %s: %s',
                    self.client_address, str(e))
            finally:
                if ENABLE_STATS:
                    JPEG_CLIENTS.dec()

        elif path == '/frame.jpg':

            t1 = datetime.now()

            try:
                self.send_response(200)
                self.send_header('Age', 0)
                self.send_header('Cache-Control', 'no-cache, private')
                self.send_header('Pragma', 'no-cache')
                self.send_header('Content-Type', 'image/jpeg')
            except Exception as e:
                logging.warning('Error sending frame: {}'.format(e))
                return

            try:
                with self.server.output.condition:
                    # no need to wait, just get the frame
                    # self.server.output.condition.wait()
                    frame = self.server.output.frame[:]
                
                self.send_header('Content-Length', len(frame))
                self.end_headers()

                self.wfile.write(frame)


                if ENABLE_STATS:
                    JPEG_BYTES_SENT.observe(len(frame))
            except Exception as e:
                logging.warning('Error getting frame %s', str(e))

            
            if ENABLE_STATS:
                t2 = datetime.now()
                JPEG_FRAME_SEND_TIME.observe((t2-t1).total_seconds())

        else:
            try:
                self.send_error(404)
                self.end_headers()
            except Exception as e:
                logging.warning('error sending 404 from path "{}": {}'.format(path, e))


class VideoConnection(object):
    def __init__(self, addr, conn):
        self.addr = addr
        self.conn = conn
        self.error = False


    def write(self, buf):
        try:
            self.conn.write(buf)

            if ENABLE_STATS:
                H264_BYTES_SENT.observe(len(buf))
        except Exception as e:
            logging.warning('error writing to {}: {}'.format(self.addr, e))
            self.error = True
            return 0
        
        return len(buf)


class VideoServer(object):
    def __init__(self, addr, port, pool_size=10):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((addr, port))
        self.sock.listen(0)

        # self.lock = threading.Lock()
        self.connections = dict()
        self.queue = queue.Queue()

        self.pool_size = pool_size
        self.threads = []
        for _ in range(self.pool_size):
            t = threading.Thread(target=self._writer)
            t.start()
            self.threads.append(t)

        self.accepter = threading.Thread(target=self._accepter)
        self.accepter.start()
        if ENABLE_STATS:
            CLIENTS.observe(0)
    
    def close(self):
        self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()
        logging.warning('socket closed, joining accepter thread')
        self.accepter.join()

        for t in self.threads:
            t.join()

        if ENABLE_STATS:
            CLIENTS.observe(0)

    def _writer(self):
        logging.info('writer started.')
        while True:
            # print('waiting for item in worker...', flush=True)
            item = self.queue.get()

            did_error = False

            if item['close']:
                break

            # print('got item from queue: {}'.format(item['addr']), flush=True)
            l = 0
            try:
                l = item['conn'].write(item['buf'])
            except Exception as e:
                logging.error('error writing to {}: {}'.format(item['addr'], e))
                did_error = True
            
            if did_error:
                try:
                    item['conn'].close()
                except Exception:
                    pass

                # with self.lock:
                del self.connections[item['addr']]

                if ENABLE_STATS:
                    CLIENTS.observe(len(self.connections))
            else:
                if ENABLE_STATS:
                    H264_BYTES_SENT.observe(l)

            self.queue.task_done()

            # print('queue size: {}'.format(self.queue.qsize()), flush=True)


    def _accepter(self):
        logging.info('accepter started')
        while True:
            try:
                (conn, addr) = self.sock.accept()
            except OSError:
                break

            logging.info('accepted: {}'.format(addr))


            # with self.lock:
            self.connections['{}:{}'.format(*addr)] = conn.makefile('wb')

            if ENABLE_STATS:
                CLIENTS.observe(len(self.connections))

            # print('connection added to list of connections', flush=True)
            
        for _ in range(self.pool_size):
            self.queue.put({'close': True})
        


    def write(self, buf):
        # print('write called: {}'.format(len(buf)), flush=True)
        # print('locked: {}'.format(self.lock.locked()), flush=True)
        conns = dict()

        # with self.lock:
            # print('acquired lock', flush=True)
        conns = self.connections.copy()

        # print('connections: {}'.format(len(conns)), flush=True)
        
        for a in conns:
            # print('adding data to queue for {}'.format(a), flush=True)

            self.queue.put({'close': False, 'buf': buf, 'addr': a, 'conn': conns[a]})

        # print('waiting to join queue', flush=True)
        self.queue.join()

        return len(buf)
        
class WebServer(socketserver.ThreadingMixIn, server.HTTPServer):
    allow_reuse_address = True
    # daemon_threads = True

    def __init__(self, output, *args, **kwargs):
        super(WebServer, self).__init__(*args, **kwargs)
        self.output = output
        self.exp = re.compile('^([^\?]+)\??.*$')
        
        # self.motionOutput = motionOutput

# Accept a single connection and make a file-like object out of it
# connection = server_socket.accept()[0].makefile('wb')

def main(args):
    numeric_level = getattr(logging, args.log.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError('Invalid log level: %s' % args.log)
    logging.basicConfig(level=numeric_level, format='%(asctime)s %(levelname)s %(message)s')


    logging.info('starting picamera')

    if ENABLE_STATS:
        INFO.info({'host': socket.gethostname(), 'version':'0.1.0'})


    camera = picamera.PiCamera(resolution=(args.width,args.height), framerate=args.framerate)
    #camera.resolution = (1440, 1080)
    # camera.resolution = (1640, 1248)
    # camera.framerate = 24

    logging.info('creating socket server')
    # server = SocketServer('0.0.0.0', args.video_port)
    server = VideoServer('0.0.0.0', args.video_port)

    logging.info('creating mjpeg outputer')
    output = StreamingOutput()

    logging.info('creating webserver')
    webServer = WebServer(output, ('', args.http_port), WebHandler)

    try:
        logging.info('starting mjpeg recorder')
        camera.start_recording(output, format='mjpeg', splitter_port=2, resize=(args.jpeg_width,args.jpeg_height))

        logging.info('starting h264 recorder')
        camera.start_recording(server, format='h264', level=args.h264_level, profile=args.h264_profile, intra_refresh='cyclic', inline_headers=True, sps_timing=True)

        logging.info('starting webserver')
        webServer.serve_forever()
    except KeyboardInterrupt:
        pass

    print('stopping recording')
    camera.stop_recording()
    print('stopping recording splitter_port=2')
    camera.stop_recording(splitter_port=2)
    print('server close')
    server.close()

    print('camera close')
    camera.close()
    print('webServer shutdown')
    webServer.shutdown()
    print('done')

    # camera.stop_recording()
    # server.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="picamera wrapper for JPEG, MJPEG, H264 and motion analysis")
    parser.add_argument("--http_port", default=8888, help="port to serve http for frames and mjpeg")
    parser.add_argument("--video_port", default=8000, help="port to listen for h264 video frames")
    parser.add_argument("--width", default=1440, help="full frame width for motion analysis and h264")
    parser.add_argument("--height", default=1080, help="full frame height for motion analysis and h264")
    parser.add_argument("--jpeg_width", default=640, help="frame width for JPEG and MJPEG", type=int)
    parser.add_argument("--jpeg_height", default=480, help="frame height for JPEG and MJPEG", type=int)
    parser.add_argument("--h264_level", default="4.2", help="h264 level for picamera library")
    parser.add_argument("--h264_profile", default="high", help="h264 profile for picamera library")
    parser.add_argument("--framerate", default=24, help="video framerate", type=int)
    parser.add_argument("--log", default="INFO", help="logging level")
    

    args = parser.parse_args()
    logging.info(args)
    main(args)

