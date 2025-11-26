import time
import io
import sys
import logging
from http import server
from threading import Condition
import socketserver
import threading
import argparse


from picamera2 import Picamera2
from picamera2.encoders import H264Encoder, MJPEGEncoder, JpegEncoder
from picamera2.outputs import CircularOutput, FileOutput


PAGE = """\
<html>
<head>
<title>picamera2 MJPEG streaming demo</title>
</head>
<body>
<h1>Picamera2 MJPEG Streaming Demo</h1>
<img src="stream.mjpg" width="640" height="480" />
</body>
</html>
"""

class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame_number = 0
        self.frame = None
        self.condition = Condition()

    def write(self, buf):
        with self.condition:
            self.frame_number = (self.frame_number + 1) % 1e20
            self.frame = buf[:]             # copy the buffer here
            self.condition.notify_all()


class StreamingHandler(server.BaseHTTPRequestHandler):
    m_kill_message = 'killing server'
    m_pause_message = 'pausing server'
    m_restart_message = 'restarting server'

    def do_GET(self):
        if self.path == '/':
            self.send_response(301)
            self.send_header('Location', '/index.html')
            self.end_headers()
        elif self.path == '/kill':
            self.send_response(200)
            self.send_header('Age', 0)
            self.send_header('Pragma', 'no-cache')
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', len(self.m_kill_message))
            self.end_headers()

            self.wfile.write(self.m_kill_message)
            self.server.shutdown()
            sys.exit(0)

            # with self.server.frame_output.condition:
            #     self.server.frame_output.condition.wait()
            #     self.server.shutdown()
            #     sys.exit(0)
        # elif self.path == '/pause':
        #     self.send_response(200)
        #     self.send_header('Age', 0)
        #     self.send_header('Pragma', 'no-cache')
        #     self.send_header('Content-Type', 'text/plain')
        #     self.send_header('Content-Length', len(self.m_pause_message))
        #     self.end_headers()

        #     self.wfile.write(self.m_kill_message)
        #     with self.server.frame_output.condition:
        #         self.server.frame_output.condition.wait()

        elif self.path == '/index.html':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', len(self.server.page_content))
            self.end_headers()
            self.wfile.write(self.server.page_content)
        elif self.path == '/frame.jpg':
            with self.server.frame_output.condition:
                self.server.frame_output.condition.wait()
                frame = self.server.frame_output.frame

            self.send_response(200)
            self.send_header('Age', 0)
            self.send_header('Cache-Control', 'no-cache, private')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Content-Length', len(frame))
            self.end_headers()

            self.wfile.write(frame)
        elif self.path == '/stream.mjpg':

            self.send_response(200)
            self.send_header('Age', 0)
            self.send_header('Cache-Control', 'no-cache, private')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=FRAME')
            self.end_headers()
            try:
                frame_number = -1
                while True:
                    # time.sleep(1.0 / 15.0)
                    with self.server.frame_output.condition:
                        self.server.frame_output.condition.wait()
                        if frame_number == self.server.frame_output.frame_number:
                            continue

                        frame_number = self.server.frame_output.frame_number
                        frame = self.server.frame_output.frame

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
        else:
            self.send_error(404)
            self.end_headers()

class StreamingServer(socketserver.ThreadingMixIn, server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, page_content, frame_output, *args, **kwargs):
        socketserver.ThreadingMixIn.__init__(self)
        server.HTTPServer.__init__(self, *args, **kwargs)

        self.frame_output = frame_output
        self.page_content = page_content.encode('utf-8')



def main(address, port, width, height, format, jpeg_threads):
    picam2 = Picamera2()
    config = picam2.create_video_configuration(
        main={"size": (width, height), "format": format }, 
        controls={"FrameDurationLimits": (100000,100000)})
    picam2.configure(config)

    output = StreamingOutput()
    # maybe this:
    # output = StreamingOutput(picam2, JpegEncoder(num_threads=jpeg_threads))
    # output.start()
    ## output.stop()

    picam2.start_recording(JpegEncoder(num_threads=jpeg_threads), FileOutput(output))
    server = StreamingServer(PAGE, output, (address, port), StreamingHandler)
    server.serve_forever()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='picamera2 wrapper for mjpeg and frames')
    parser.add_argument('--address', default='0.0.0.0', help='listen on address')
    parser.add_argument('--port', default=8080, type=int, help='listen on port')
    parser.add_argument('--width', default=640, type=int, help='width of video')
    parser.add_argument('--height', default=480, type=int, help='height of video')
    parser.add_argument('--format', default='YUV420', type=str, help='format of video')
    parser.add_argument('--jpeg_threads', default=2, type=int, help='threads to dedicate to jpeg encoding')

    main(**vars(parser.parse_args()))
