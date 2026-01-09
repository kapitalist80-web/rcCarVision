#!/usr/bin/env python3
"""
LEGO 42140 Web-Interface mit Kamera-Stream
Steuerung + Live-Video über Browser/Handy
"""

from flask import Flask, render_template, Response, jsonify, request
import asyncio
from bleak import BleakClient
import threading
import json
from datetime import datetime
import cv2
import numpy as np
import time

app = Flask(__name__)

# Kamera Setup
camera = None
camera_lock = threading.Lock()
camera_enabled = False
camera_type = None  # 'picamera2' oder 'opencv'
camera_fps = 0
last_frame_time = time.time()

# Autonomer Modus
autonomous_mode = False
autonomous_thread = None
obstacle_detection_enabled = True

# Hinderniserkennung Parameter
OBSTACLE_THRESHOLD = 3000  # Schwellwert für Hindernisse (Pixel)
TURN_DURATION = 0.8  # Sekunden für Drehung
EDGE_LOWER = 50  # Canny Edge Detection unterer Wert
EDGE_UPPER = 150  # Canny Edge Detection oberer Wert

def init_camera():
    """Kamera initialisieren - versucht erst Picamera2, dann USB"""
    global camera, camera_enabled, camera_type
    
    # Versuch 1: Picamera2 (Raspberry Pi Kamera)
    try:
        from picamera2 import Picamera2
        
        print("Versuche Picamera2...")
        camera = Picamera2()
        
        config = camera.create_preview_configuration(
            main={"size": (640, 480), "format": "RGB888"}
        )
        camera.configure(config)
        
        print("Starte Kamera...")
        camera.start()
        
        camera_enabled = True
        camera_type = 'picamera2'
        print("✓ Picamera2 initialisiert (640x480)")
        
        # Warmup - wichtig!
        time.sleep(2)
        
        # Test-Frame
        try:
            test_frame = camera.capture_array()
            print(f"✓ Test-Frame: {test_frame.shape}")
        except Exception as e:
            print(f"⚠ Test-Frame Warnung: {e}")
        
        return True
        
    except ImportError as e:
        print(f"Picamera2 nicht verfügbar: {e}")
        print("Versuche USB-Webcam...")
    except Exception as e:
        print(f"Picamera2 Fehler: {e}")
        print("Versuche USB-Webcam...")
    
    # Versuch 2: USB-Webcam via OpenCV
    try:
        print("Versuche USB-Webcam...")
        camera = cv2.VideoCapture(0)
        
        if not camera.isOpened():
            raise Exception("Kann Webcam nicht öffnen")
        
        # Auflösung setzen
        camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        camera.set(cv2.CAP_PROP_FPS, 30)
        
        # Test-Frame
        ret, frame = camera.read()
        if not ret or frame is None:
            raise Exception("Kann kein Bild aufnehmen")
        
        camera_enabled = True
        camera_type = 'opencv'
        print(f"✓ USB-Webcam initialisiert ({frame.shape[1]}x{frame.shape[0]})")
        return True
        
    except Exception as e:
        print(f"✗ Webcam-Fehler: {e}")
        camera_enabled = False
        camera_type = None
        camera = None
        return False


def generate_frames():
    """Frame-Generator für Video-Stream"""
    global camera_fps, last_frame_time, camera_type, camera_enabled
    frame_count = 0
    fps_update_time = time.time()
    error_count = 0
    
    print(f"Stream gestartet. Kamera: {camera_type}, Enabled: {camera_enabled}")
    
    while True:
        if not camera_enabled or camera is None:
            # Dummy-Frame wenn Kamera nicht verfügbar
            dummy = create_dummy_frame(f"Kamera nicht verfügbar\nType: {camera_type}\nEnabled: {camera_enabled}")
            ret, buffer = cv2.imencode('.jpg', dummy, [cv2.IMWRITE_JPEG_QUALITY, 85])
            frame = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            time.sleep(0.1)
            continue
        
        try:
            with camera_lock:
                # Frame holen (abhängig vom Kamera-Typ)
                if camera_type == 'picamera2':
                    frame = camera.capture_array()
                    # Picamera2 liefert RGB, OpenCV braucht BGR
                    frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                    
                elif camera_type == 'opencv':
                    ret, frame = camera.read()
                    if not ret or frame is None:
                        raise Exception("Kein Frame von Webcam")
                    # OpenCV liefert schon BGR
                    
                else:
                    raise Exception(f"Unbekannter Kamera-Typ: {camera_type}")
                
                # Hinderniserkennung anwenden (wenn aktiviert)
                if obstacle_detection_enabled:
                    left, center, right, frame = detect_obstacles(frame)
                    # Entscheidung anzeigen
                    direction = decide_direction(left, center, right)
                    if autonomous_mode:
                        cv2.putText(frame, f"AUTO: {direction.upper()}", (10, 120),
                                   cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
                
                # FPS berechnen
                frame_count += 1
                current_time = time.time()
                if current_time - fps_update_time >= 1.0:
                    camera_fps = frame_count / (current_time - fps_update_time)
                    frame_count = 0
                    fps_update_time = current_time
                    if error_count > 0:
                        print(f"FPS: {camera_fps:.1f}, Fehler letzte Sekunde: {error_count}")
                        error_count = 0
                
                # Info auf Frame zeichnen
                cv2.putText(frame, f"FPS: {camera_fps:.1f}", (10, 30),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                cv2.putText(frame, f"{frame.shape[1]}x{frame.shape[0]}", (10, 60),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                cv2.putText(frame, f"Type: {camera_type}", (10, 90),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
                
                # Autonomer Modus Indicator
                if autonomous_mode:
                    cv2.circle(frame, (frame.shape[1] - 30, 30), 15, (0, 255, 0), -1)
                    cv2.putText(frame, "AUTO", (frame.shape[1] - 75, 35),
                               cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
                
                # Frame zu JPEG konvertieren
                ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if not ret:
                    raise Exception("JPEG encode fehlgeschlagen")
                    
                frame_bytes = buffer.tobytes()
                
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
            
            # Kleine Pause um CPU zu entlasten (ca. 30 FPS max)
            time.sleep(0.01)
            
        except Exception as e:
            error_count += 1
            if error_count <= 3:  # Nur erste 3 Fehler loggen
                print(f"Frame-Fehler: {e}")
            
            # Bei Fehler Dummy-Frame
            dummy = create_dummy_frame(f"Frame-Fehler:\n{str(e)[:50]}")
            ret, buffer = cv2.imencode('.jpg', dummy, [cv2.IMWRITE_JPEG_QUALITY, 85])
            frame = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            time.sleep(0.1)


def create_dummy_frame(error_msg="Kamera nicht verfügbar"):
    """Dummy-Frame erstellen wenn Kamera nicht verfügbar"""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    
    # Hintergrund leicht aufhellen
    frame[:] = (30, 30, 30)
    
    # Fehler-Text
    text_lines = error_msg.split('\n')
    y_offset = 200
    for line in text_lines:
        cv2.putText(frame, line, (50, y_offset),
                   cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        y_offset += 40
    
    # Hilfetext
    cv2.putText(frame, "Prüfe:", (50, 320),
               cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150, 150, 150), 1)
    cv2.putText(frame, "1. Kamera angeschlossen?", (50, 350),
               cv2.FONT_HERSHEY_SIMPLEX, 0.4, (150, 150, 150), 1)
    cv2.putText(frame, "2. picamera2 installiert?", (50, 375),
               cv2.FONT_HERSHEY_SIMPLEX, 0.4, (150, 150, 150), 1)
    cv2.putText(frame, "3. USB-Webcam vorhanden?", (50, 400),
               cv2.FONT_HERSHEY_SIMPLEX, 0.4, (150, 150, 150), 1)
    
    return frame


def detect_obstacles(frame):
    """
    Hinderniserkennung mit OpenCV
    Returns: (left_count, center_count, right_count, processed_frame)
    """
    # Kopie für Visualisierung
    display_frame = frame.copy()
    
    # Graustufen
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    
    # Gaußscher Weichzeichner (Rauschen reduzieren)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    
    # Canny Edge Detection
    edges = cv2.Canny(blur, EDGE_LOWER, EDGE_UPPER)
    
    # Region of Interest (untere 60% des Bildes)
    height, width = edges.shape
    roi_height = int(height * 0.6)
    roi = edges[height - roi_height:height, 0:width]
    
    # Frame in 3 Zonen teilen
    zone_width = width // 3
    left_zone = roi[:, 0:zone_width]
    center_zone = roi[:, zone_width:2*zone_width]
    right_zone = roi[:, 2*zone_width:width]
    
    # Hindernisse zählen (weiße Pixel = Kanten)
    left_count = cv2.countNonZero(left_zone)
    center_count = cv2.countNonZero(center_zone)
    right_count = cv2.countNonZero(right_zone)
    
    # Visualisierung auf Display-Frame
    if obstacle_detection_enabled:
        # ROI-Bereich markieren
        roi_y = height - roi_height
        cv2.rectangle(display_frame, (0, roi_y), (width, height), (255, 255, 0), 2)
        
        # Zonen-Linien
        cv2.line(display_frame, (zone_width, roi_y), (zone_width, height), (255, 255, 0), 1)
        cv2.line(display_frame, (2*zone_width, roi_y), (2*zone_width, height), (255, 255, 0), 1)
        
        # Hindernisse visualisieren
        zone_height = roi_height
        
        # Links
        if left_count > OBSTACLE_THRESHOLD:
            cv2.rectangle(display_frame, (5, roi_y+5), (zone_width-5, height-5), (0, 0, 255), -1)
            cv2.putText(display_frame, "OBSTACLE", (20, roi_y+30), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2)
        else:
            cv2.rectangle(display_frame, (5, roi_y+5), (zone_width-5, height-5), (0, 255, 0), 2)
        
        # Mitte
        if center_count > OBSTACLE_THRESHOLD:
            cv2.rectangle(display_frame, (zone_width+5, roi_y+5), (2*zone_width-5, height-5), (0, 0, 255), -1)
            cv2.putText(display_frame, "OBSTACLE", (zone_width+20, roi_y+30), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2)
        else:
            cv2.rectangle(display_frame, (zone_width+5, roi_y+5), (2*zone_width-5, height-5), (0, 255, 0), 2)
        
        # Rechts
        if right_count > OBSTACLE_THRESHOLD:
            cv2.rectangle(display_frame, (2*zone_width+5, roi_y+5), (width-5, height-5), (0, 0, 255), -1)
            cv2.putText(display_frame, "OBSTACLE", (2*zone_width+20, roi_y+30), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2)
        else:
            cv2.rectangle(display_frame, (2*zone_width+5, roi_y+5), (width-5, height-5), (0, 255, 0), 2)
        
        # Zählwerte anzeigen
        cv2.putText(display_frame, f"L:{left_count}", (10, height-10), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)
        cv2.putText(display_frame, f"C:{center_count}", (zone_width+10, height-10), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)
        cv2.putText(display_frame, f"R:{right_count}", (2*zone_width+10, height-10), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)
    
    return left_count, center_count, right_count, display_frame


def decide_direction(left, center, right):
    """
    Entscheidung basierend auf Hindernissen
    Returns: direction ('forward', 'left', 'right', 'backward')
    """
    # Hindernisse prüfen
    obstacle_left = left > OBSTACLE_THRESHOLD
    obstacle_center = center > OBSTACLE_THRESHOLD
    obstacle_right = right > OBSTACLE_THRESHOLD
    
    # Entscheidungslogik
    if not obstacle_center and not obstacle_left and not obstacle_right:
        # Alles frei - vorwärts
        return 'forward'
    
    elif obstacle_center:
        # Hindernis vorne
        if not obstacle_left and not obstacle_right:
            # Links und rechts frei - wähle Seite mit weniger Hindernissen
            return 'left' if left < right else 'right'
        elif not obstacle_left:
            # Nur links frei
            return 'left'
        elif not obstacle_right:
            # Nur rechts frei
            return 'right'
        else:
            # Überall Hindernisse - rückwärts
            return 'backward'
    
    elif obstacle_left:
        # Hindernis links - nach rechts
        return 'right'
    
    elif obstacle_right:
        # Hindernis rechts - nach links
        return 'left'
    
    else:
        # Default - vorwärts
        return 'forward'


def autonomous_drive_loop():
    """Autonomer Fahr-Loop"""
    global autonomous_mode
    
    print("Autonomer Modus gestartet")
    
    while autonomous_mode:
        if not camera_enabled or camera is None:
            time.sleep(0.1)
            continue
        
        try:
            # Frame holen
            with camera_lock:
                if camera_type == 'picamera2':
                    frame = camera.capture_array()
                    frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                elif camera_type == 'opencv':
                    ret, frame = camera.read()
                    if not ret:
                        continue
                else:
                    continue
            
            # Hindernisse erkennen
            left, center, right, _ = detect_obstacles(frame)
            
            # Richtung entscheiden
            direction = decide_direction(left, center, right)
            
            # Fahren
            if direction == 'forward':
                hub.drive_forward(40)  # Moderate Geschwindigkeit
                time.sleep(0.3)
                
            elif direction == 'left':
                hub.turn_left(50)
                time.sleep(TURN_DURATION)
                
            elif direction == 'right':
                hub.turn_right(50)
                time.sleep(TURN_DURATION)
                
            elif direction == 'backward':
                hub.drive_backward(40)
                time.sleep(0.5)
                # Nach rückwärts drehen
                hub.turn_right(50)
                time.sleep(TURN_DURATION)
            
            # Kurze Pause
            time.sleep(0.1)
            
        except Exception as e:
            print(f"Autonomer Modus Fehler: {e}")
            time.sleep(0.5)
    
    # Stoppen wenn Modus beendet
    hub.stop()
    print("Autonomer Modus beendet")


class LegoPoweredUpHub:
    """Controller für LEGO Powered Up Hub"""
    
    HUB_MAC = "90:84:2B:70:3E:B6"
    CHARACTERISTIC_UUID = "00001624-1212-efde-1623-785feabcd123"
    
    PORT_A = 0x00
    PORT_B = 0x01
    
    def __init__(self):
        self.client = None
        self.connected = False
        self.loop = None
        self.thread = None
        self.current_speed = 50
        self.status = "Disconnected"
        self.last_command = "None"
        
    def start_async_loop(self):
        """Async loop in separatem Thread"""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()
    
    def run_async(self, coro):
        """Coroutine im async Thread ausführen"""
        if not self.loop:
            return None
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout=5)
        except Exception as e:
            print(f"Error: {e}")
            return None
    
    def connect(self):
        """Verbindung herstellen"""
        if not self.thread:
            self.thread = threading.Thread(target=self.start_async_loop, daemon=True)
            self.thread.start()
        
        async def _connect():
            try:
                self.client = BleakClient(self.HUB_MAC, timeout=10.0)
                await self.client.connect()
                self.connected = True
                self.status = "Connected"
                return True
            except Exception as e:
                self.status = f"Error: {e}"
                return False
        
        return self.run_async(_connect())
    
    def disconnect(self):
        """Verbindung trennen"""
        async def _disconnect():
            if self.client and self.connected:
                await self.stop_all_motors()
                await self.client.disconnect()
                self.connected = False
                self.status = "Disconnected"
        
        self.run_async(_disconnect())
        
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
    
    async def send_message(self, message):
        """Nachricht senden"""
        if not self.connected or not self.client:
            return False
        try:
            await self.client.write_gatt_char(
                self.CHARACTERISTIC_UUID,
                bytearray(message),
                response=False
            )
            return True
        except:
            return False
    
    async def set_motor_speed(self, port, speed):
        """Motor-Geschwindigkeit setzen"""
        speed = max(-100, min(100, speed))
        speed_byte = speed if speed >= 0 else 256 + speed
        
        message = [
            0x08, 0x00, 0x81, port,
            0x11, 0x51, 0x00, speed_byte
        ]
        
        await self.send_message(message)
    
    async def stop_all_motors(self):
        """Alle Motoren stoppen"""
        await self.set_motor_speed(self.PORT_A, 0)
        await self.set_motor_speed(self.PORT_B, 0)
    
    def drive_forward(self, speed=None):
        """Vorwärts fahren"""
        if speed is None:
            speed = self.current_speed
        
        async def _drive():
            await self.set_motor_speed(self.PORT_A, speed)
            await self.set_motor_speed(self.PORT_B, -speed)
        
        self.last_command = f"Forward ({speed}%)"
        self.run_async(_drive())
    
    def drive_backward(self, speed=None):
        """Rückwärts fahren"""
        if speed is None:
            speed = self.current_speed
        
        async def _drive():
            await self.set_motor_speed(self.PORT_A, -speed)
            await self.set_motor_speed(self.PORT_B, speed)
        
        self.last_command = f"Backward ({speed}%)"
        self.run_async(_drive())
    
    def turn_left(self, speed=None):
        """Links drehen"""
        if speed is None:
            speed = self.current_speed
        
        async def _drive():
            await self.set_motor_speed(self.PORT_A, int(speed * 0.3))
            await self.set_motor_speed(self.PORT_B, -speed)
        
        self.last_command = f"Left ({speed}%)"
        self.run_async(_drive())
    
    def turn_right(self, speed=None):
        """Rechts drehen"""
        if speed is None:
            speed = self.current_speed
        
        async def _drive():
            await self.set_motor_speed(self.PORT_A, speed)
            await self.set_motor_speed(self.PORT_B, int(-speed * 0.3))
        
        self.last_command = f"Right ({speed}%)"
        self.run_async(_drive())
    
    def pivot_left(self, speed=None):
        """Pivot links"""
        if speed is None:
            speed = self.current_speed
        
        async def _drive():
            await self.set_motor_speed(self.PORT_A, -speed)
            await self.set_motor_speed(self.PORT_B, -speed)
        
        self.last_command = f"Pivot Left ({speed}%)"
        self.run_async(_drive())
    
    def pivot_right(self, speed=None):
        """Pivot rechts"""
        if speed is None:
            speed = self.current_speed
        
        async def _drive():
            await self.set_motor_speed(self.PORT_A, speed)
            await self.set_motor_speed(self.PORT_B, speed)
        
        self.last_command = f"Pivot Right ({speed}%)"
        self.run_async(_drive())
    
    def stop(self):
        """Stoppen"""
        async def _stop():
            await self.stop_all_motors()
        
        self.last_command = "Stop"
        self.run_async(_stop())


# Globale Hub-Instanz
hub = LegoPoweredUpHub()


# Flask Routes
@app.route('/')
def index():
    """Hauptseite"""
    return render_template('index.html')


@app.route('/video_feed')
def video_feed():
    """Video-Stream Route"""
    return Response(generate_frames(),
                   mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/api/camera/toggle', methods=['POST'])
def api_camera_toggle():
    """Kamera ein/ausschalten"""
    global camera_enabled
    
    data = request.json
    enable = data.get('enable', True)
    
    if enable and not camera_enabled:
        success = init_camera()
        return jsonify({'success': success, 'enabled': camera_enabled})
    elif not enable and camera_enabled:
        camera_enabled = False
        return jsonify({'success': True, 'enabled': False})
    
    return jsonify({'success': True, 'enabled': camera_enabled})


@app.route('/api/camera/status')
def api_camera_status():
    """Kamera-Status"""
    return jsonify({
        'enabled': camera_enabled,
        'fps': round(camera_fps, 1)
    })


@app.route('/api/connect', methods=['POST'])
def api_connect():
    """Verbindung zum Hub herstellen"""
    success = hub.connect()
    return jsonify({
        'success': success,
        'status': hub.status,
        'connected': hub.connected
    })


@app.route('/api/disconnect', methods=['POST'])
def api_disconnect():
    """Verbindung trennen"""
    hub.disconnect()
    return jsonify({
        'success': True,
        'status': hub.status,
        'connected': hub.connected
    })


@app.route('/api/status')
def api_status():
    """Status abfragen"""
    return jsonify({
        'connected': hub.connected,
        'status': hub.status,
        'speed': hub.current_speed,
        'last_command': hub.last_command,
        'camera_enabled': camera_enabled,
        'camera_fps': round(camera_fps, 1),
        'timestamp': datetime.now().isoformat()
    })


@app.route('/api/control', methods=['POST'])
def api_control():
    """Fahrzeug steuern"""
    data = request.json
    command = data.get('command')
    speed = data.get('speed', hub.current_speed)
    
    if not hub.connected:
        return jsonify({'success': False, 'error': 'Not connected'}), 400
    
    commands = {
        'forward': hub.drive_forward,
        'backward': hub.drive_backward,
        'left': hub.turn_left,
        'right': hub.turn_right,
        'pivot_left': hub.pivot_left,
        'pivot_right': hub.pivot_right,
        'stop': hub.stop
    }
    
    if command in commands:
        if command == 'stop':
            commands[command]()
        else:
            commands[command](speed)
        return jsonify({'success': True, 'command': command})
    
    return jsonify({'success': False, 'error': 'Invalid command'}), 400


@app.route('/api/speed', methods=['POST'])
def api_speed():
    """Geschwindigkeit ändern"""
    data = request.json
    speed = data.get('speed', 50)
    hub.current_speed = max(10, min(100, speed))
    return jsonify({'success': True, 'speed': hub.current_speed})


@app.route('/api/autonomous/toggle', methods=['POST'])
def api_autonomous_toggle():
    """Autonomen Modus ein/ausschalten"""
    global autonomous_mode, autonomous_thread
    
    data = request.json
    enable = data.get('enable', False)
    
    if enable and not autonomous_mode:
        # Autonomen Modus starten
        if not hub.connected:
            return jsonify({'success': False, 'error': 'Hub nicht verbunden'}), 400
        
        autonomous_mode = True
        autonomous_thread = threading.Thread(target=autonomous_drive_loop, daemon=True)
        autonomous_thread.start()
        
        return jsonify({'success': True, 'autonomous': True})
        
    elif not enable and autonomous_mode:
        # Autonomen Modus stoppen
        autonomous_mode = False
        if autonomous_thread:
            autonomous_thread.join(timeout=2)
        hub.stop()
        
        return jsonify({'success': True, 'autonomous': False})
    
    return jsonify({'success': True, 'autonomous': autonomous_mode})


@app.route('/api/autonomous/status')
def api_autonomous_status():
    """Status des autonomen Modus"""
    return jsonify({
        'autonomous': autonomous_mode,
        'detection_enabled': obstacle_detection_enabled,
        'threshold': OBSTACLE_THRESHOLD
    })


@app.route('/api/detection/toggle', methods=['POST'])
def api_detection_toggle():
    """Hinderniserkennung-Visualisierung ein/ausschalten"""
    global obstacle_detection_enabled
    
    data = request.json
    obstacle_detection_enabled = data.get('enable', True)
    
    return jsonify({'success': True, 'enabled': obstacle_detection_enabled})


@app.route('/api/detection/threshold', methods=['POST'])
def api_detection_threshold():
    """Schwellwert für Hinderniserkennung ändern"""
    global OBSTACLE_THRESHOLD
    
    data = request.json
    threshold = data.get('threshold', 3000)
    OBSTACLE_THRESHOLD = max(500, min(10000, threshold))
    
    return jsonify({'success': True, 'threshold': OBSTACLE_THRESHOLD})


if __name__ == '__main__':
    print("="*50)
    print("LEGO 42140 Web Control + Camera")
    print("="*50)
    
    # Kamera initialisieren
    print("\nInitialisiere Kamera...")
    init_camera()
    
    print("\nStarte Server...")
    print("\nZugriff über:")
    print("  Lokal:  http://localhost:5000")
    print("  Handy:  http://<raspberry-pi-ip>:5000")
    print("\nRaspberry Pi IP-Adresse mit 'hostname -I' herausfinden")
    print("\nStrg+C zum Beenden")
    print("="*50)
    
    try:
        # Server auf allen Interfaces starten für Handy-Zugriff
        app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
    except KeyboardInterrupt:
        print("\nBeende Server...")
        hub.disconnect()
        if camera:
            camera.stop()
