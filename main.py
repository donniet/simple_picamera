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
import redis
import redistimeseries.client
from datetime import datetime, timedelta
import dateutil.tz


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
    def __init__(self, camera, magnitude, threshold, notifier, timeseries, timeseries_key):
        super(DetectMotion, self).__init__(camera, size=None)
        self.total_motion = 0.
        self.magnitude = magnitude
        self.threshold = threshold
        self.notifier = notifier
        self.timeseries = timeseries
        self.timeseries_key = timeseries_key

    def analyze(self, a):
        a = np.sqrt(
            np.square(a['x'].astype(np.float)) +
            np.square(a['y'].astype(np.float))
            ).clip(0, 255).astype(np.uint8)
        # If there're more than 10 vectors with a magnitude greater
        # than 60, then say we've detected motion

        self.total_motion = (a > self.magnitude).sum()
        timestamp = int((datetime.now(tz=dateutil.tz.tzutc()) - EPOCH) / timedelta(milliseconds=1))
        self.timeseries.add(self.timeseries_key, timestamp, float(self.total_motion))

        # print('total_motion: {}'.format(self.total_motion), flush=True)

        if self.total_motion > self.threshold:
            print('Motion detected: {}'.format(self.total_motion), flush=True)
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
        self.timestamp = 0
        self.interval = 60


    def notify_thread(self):
        self.condition.acquire()

        while not self.completed:
            self.condition.wait()

            if self.completed:
                break
            
            n = time.time()
            if n - self.timestamp < self.interval:
                continue

            self.timestamp = n

            # no need to hold the lock while we make the request
            self.condition.release()

            try:
                print('sending notification', flush=True)
                requests.post(self.url, data=self.data)
            except Exception as e:
                print('Exception when notifying: {}'.format(e), flush=True)
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

class VideoConnection(object):
    def __init__(self, addr, conn):
        self.addr = addr
        self.conn = conn
        self.error = False

    def write(self, buf):
        try:
            self.conn.write(buf)
        except Exception as e:
            print('error writing to {}: {}'.format(self.addr, e))
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
    
    def close(self):
        self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()
        print('socket closed, joining accepter thread', flush=True)
        self.accepter.join()

        for t in self.threads:
            t.join()

    def _writer(self):
        print('writer started.', flush=True)
        while True:
            # print('waiting for item in worker...', flush=True)
            item = self.queue.get()

            if item['close']:
                break

            # print('got item from queue: {}'.format(item['addr']), flush=True)

            did_error = False
            try:
                item['conn'].write(item['buf'])
            except Exception as e:
                print('error writing to {}: {}'.format(item['addr'], e))
                did_error = True
            
            if did_error:
                item['conn'].close()
                # with self.lock:
                del self.connections[item['addr']]

            self.queue.task_done()

            # print('queue size: {}'.format(self.queue.qsize()), flush=True)


    def _accepter(self):
        print('accepter started', flush=True)
        while True:
            try:
                (conn, addr) = self.sock.accept()
            except OSError:
                break

            print('accepted: {}'.format(addr), flush=True)

            # with self.lock:
            self.connections['{}:{}'.format(*addr)] = conn.makefile('wb')

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
        # self.motionOutput = motionOutput

# Accept a single connection and make a file-like object out of it
# connection = server_socket.accept()[0].makefile('wb')

def main(args):
    print('starting picamera', flush=True)

    red = redistimeseries.client.Client(host=args.redis_host, port=args.redis_port)
    motion_key = 'motion:{}:{}x{}@{}:{}'.format(args.redis_prefix, args.width, args.height, args.framerate, args.macroblock_magnitude)
    try:
        red.create(motion_key, retention_msecs=86400000, labels={
            'width': args.width,
            'height': args.height,
            'framerate': args.framerate,
            'macroblock_magnitude': args.macroblock_magnitude,
            'motion_threshold': args.motion_threshold
        })
    except redis.exceptions.ResponseError:
        pass
    



    camera = picamera.PiCamera(resolution=(args.width,args.height), framerate=args.framerate)
    #camera.resolution = (1440, 1080)
    # camera.resolution = (1640, 1248)
    # camera.framerate = 24

    print('creating socket server', flush=True)
    # server = SocketServer('0.0.0.0', args.video_port)
    server = VideoServer('0.0.0.0', args.video_port)

    print('creating mjpeg outputer', flush=True)
    output = StreamingOutput()

    print('creating notifier', flush=True)
    notifier = Notifier(url=args.notify, data=args.notify_data)
    # motionOutput = MotionOutput()
    detectMotion = DetectMotion(camera, magnitude=args.macroblock_magnitude, threshold=args.motion_threshold, notifier=notifier, timeseries=red, timeseries_key=motion_key)

    print('creating webserver', flush=True)
    webServer = WebServer(output, ('', args.http_port), WebHandler)
        

    try:
        print('starting mjpeg recorder', flush=True)
        camera.start_recording(output, format='mjpeg', splitter_port=2, resize=(args.jpeg_width,args.jpeg_height))

        print('starting h264 recorder', flush=True)
        camera.start_recording(server, format='h264', level=args.h264_level, profile=args.h264_profile, intra_refresh='cyclic', inline_headers=True, sps_timing=True, motion_output=detectMotion)

        print('starting webserver', flush=True)
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

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="picamera wrapper for JPEG, MJPEG, H264 and motion analysis")
    parser.add_argument("--notify", default="http://mirror.local:9080/", help="url to post a notification to")
    parser.add_argument("--notify_data", default='"on"', help="data to post to notification url")
    parser.add_argument("--notify_interval", default=60, help="throttle (sec) between notifications")
    parser.add_argument("--http_port", default=8888, help="port to serve http for frames and mjpeg")
    parser.add_argument("--video_port", default=8000, help="port to listen for h264 video frames")
    parser.add_argument("--macroblock_magnitude", default=60, help="macroblock motion vector minimum magnitude to flag as motion")
    parser.add_argument("--motion_threshold", default=10, help="number motion macroblocks to trigger full frame motion")
    parser.add_argument("--width", default=1440, help="full frame width for motion analysis and h264")
    parser.add_argument("--height", default=1080, help="full frame height for motion analysis and h264")
    parser.add_argument("--jpeg_width", default=640, help="frame width for JPEG and MJPEG")
    parser.add_argument("--jpeg_height", default=480, help="frame height for JPEG and MJPEG")
    parser.add_argument("--h264_level", default="4.2", help="h264 level for picamera library")
    parser.add_argument("--h264_profile", default="high", help="h264 profile for picamera library")
    parser.add_argument("--framerate", default=24, help="video framerate")
    parser.add_argument("--redis_host", default="192.168.1.45", help="redis timeseries host")
    parser.add_argument("--redis_port", default="6379", help="redis timeseries port")
    parser.add_argument('--redis_prefix', default="picamera", help="redis key prefix")

    args = parser.parse_args()
    print(args)
    main(args)

