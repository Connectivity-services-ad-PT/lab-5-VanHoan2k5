import http
import os
import csv
import json
import threading
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import paho.mqtt.client as mqtt

# --- 1. ĐỌC CẤU HÌNH BIẾN MÔI TRƯỜNG ---
SERVICE_NAME = os.getenv("SERVICE_NAME", "iot-ingestion")
SERVICE_VERSION = os.getenv("SERVICE_VERSION", "0.5.0")
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "local-dev-token")
MQTT_BROKER_HOST = os.getenv("MQTT_BROKER_HOST", "26.146.248.73")
MQTT_BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", "1883"))

TOPIC_RAW_INPUT = "smart-campus/raw/iot/environment"
TOPIC_PROCESSED_OUTPUT = "smart-campus/events/sensor"

app = FastAPI(
    title="FIT4110 Lab 05 - IoT Ingestion Hybrid Service",
    version=SERVICE_VERSION,
    description="Dịch vụ kết hợp: Vừa nhận tin HTTP REST (Swagger) vừa tự động xử lý nghiệp vụ MQTT chạy ngầm."
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- CẤU HÌNH BẢO MẬT (ĐỂ HIỆN LẠI KHÓA XANH TRÊN SWAGGER) ---
security = HTTPBearer()

def verify_bearer_token(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)) -> Optional[str]:
    if not credentials or credentials.credentials != AUTH_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "type": "about:blank",
                "title": "Unauthorized",
                "status": 401,
                "detail": "Invalid or missing Authorization token"
            }
        )
    return credentials.credentials

# --- 2. LOGIC NẠP DEVICE REGISTRY TỪ FILE CSV ---
DEVICE_REGISTRY: Dict[str, Dict] = {}
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REGISTRY_FILE_PATH = os.path.join(BASE_DIR, "iot_device_registry.csv")

def load_device_registry():
    global DEVICE_REGISTRY
    if not os.path.exists(REGISTRY_FILE_PATH):
        print(f"⚠️ Cảnh báo: Không tìm thấy file registry tại {REGISTRY_FILE_PATH}")
        return
    try:
        with open(REGISTRY_FILE_PATH, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                dev_id = row.get("device_id")
                if dev_id:
                    DEVICE_REGISTRY[dev_id.strip().lower()] = {
                        "device_type": row.get("device_type"),
                        "location": row.get("location"),
                        "room": row.get("room"),
                        "status": row.get("status")
                    }
        print(f"✅ Đã nạp thành công {len(DEVICE_REGISTRY)} thiết bị vào bộ nhớ.")
    except Exception as e:
        print(f"❌ Lỗi khi đọc file registry: {e}")

load_device_registry()

# --- 3. ĐỊNH NGHĨA SCHEMAS CHO SWAGGER UI ---
class SensorMetric(str, Enum):
    temperature = "temperature"
    humidity = "humidity"
    motion = "motion"
    smoke = "smoke"

class SensorUnit(str, Enum):
    celsius = "celsius"
    percent = "percent"
    boolean = "boolean"
    ppm = "ppm"

class SensorReadingCreate(BaseModel):
    device_id: str = Field(..., min_length=3, examples=["esp32-lab-a101"])
    metric: SensorMetric = Field(..., examples=["temperature"])
    value: float = Field(..., ge=-40, le=80, examples=[31.2])
    unit: Optional[SensorUnit] = Field(default=None, examples=["celsius"])
    timestamp: str = Field(..., examples=["2026-06-07T14:30:10+07:00"])

class SensorReadingCreated(BaseModel):
    reading_id: str
    device_id: str
    metric: SensorMetric
    accepted: bool
    created_at: str
    device_type: Optional[str] = None
    location: Optional[str] = None
    room: Optional[str] = None
    device_status: Optional[str] = None

READINGS: List[Dict] = []

# --- 4. HÀM NGHIỆP VỤ PHÂN LOẠI CHUNG ---
def classify_sensor_reading(device_id: str, temperature_c: Optional[float], humidity_percent: Optional[float], co2_ppm: Optional[int], smoke_ppm: Optional[float], battery_percent: Optional[int]) -> dict:
    """
    Hàm phân loại tự động 4 mức độ cảnh báo (CRITICAL, HIGH, MEDIUM, LOW) 
    theo đúng bảng quy hoạch mới nhất của lớp học.
    """
    # 1. MỨC ĐỘ LOW (Thấp) - Thiết bị lạ hoặc lỗi do đọc số liệu không chuẩn
    if device_id.strip().lower() not in DEVICE_REGISTRY:
        return {"status": "invalid_device", "alert_level": "low", "reason": "invalid_device: device_not_in_registry"}
        
    if temperature_c is None or humidity_percent is None:
        return {"status": "sensor_error", "alert_level": "low", "reason": "sensor_error: measurement_data_is_null"}

    # 2. MỨC ĐỘ CRITICAL (Khẩn cấp) - Hỏa hoạn/Khói cháy hoặc Nhiệt độ quá hiểm họa
    if (smoke_ppm is not None and smoke_ppm >= 1.0) or temperature_c >= 50.0:
        return {"status": "danger", "alert_level": "critical", "reason": "critical: fire_or_extreme_heat_detected"}

    # 3. MỨC ĐỘ HIGH (Cao) - Vấn đề môi trường phòng máy vượt ngưỡng nguy hiểm
    if temperature_c >= 40.0 or (co2_ppm is not None and co2_ppm >= 1800):
        return {"status": "danger", "alert_level": "high", "reason": "high: server_room_temperature_exceeded_threshold"}

    # 4. MỨC ĐỘ MEDIUM (Trung bình) - Cảm biến yếu pin hoặc cảnh báo vận hành thường quy
    if (battery_percent is not None and battery_percent < 20) or temperature_c >= 35.0 or humidity_percent >= 85.0:
        reasons = []
        if battery_percent is not None and battery_percent < 20: reasons.append("low_battery_warning")
        if temperature_c >= 35.0: reasons.append("temperature_warning")
        if humidity_percent >= 85.0: reasons.append("humidity_high")
        return {"status": "warning", "alert_level": "medium", "reason": ", ".join(reasons)}

    # 5. MỨC ĐỘ LOW (Thấp) - Trạng thái bình thường không có lỗi
    return {"status": "normal", "alert_level": "low", "reason": "all_metrics_normal"}


# --- 5. LUỒNG XỬ LÝ TRUYỀN TIN NGẦM MQTT ---
mqtt_client = mqtt.Client()

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"🚀 Kết nối MQTT Broker thành công! Đang subscribe: {TOPIC_RAW_INPUT}")
        client.subscribe(TOPIC_RAW_INPUT)

def on_message(client, userdata, msg):
    try:
        raw_payload = json.loads(msg.payload.decode("utf-8"))
        required_fields = ["event_id", "timestamp", "device_id", "temperature_c", "humidity_percent", "motion_detected"]
        if not all(field in raw_payload for field in required_fields):
            return

        analysis = classify_sensor_reading(
            raw_payload.get("device_id", ""),
            raw_payload.get("temperature_c"),
            raw_payload.get("humidity_percent"),
            raw_payload.get("co2_ppm"),
            raw_payload.get("smoke_ppm"),
            raw_payload.get("battery_percent")
        )

        processed_event = {
            "event_type": "sensor.reading.processed",
            "source_service": "team-iot",
            "raw_event_id": raw_payload.get("event_id"),
            "device_id": raw_payload.get("device_id"),
            "location": raw_payload.get("location"),
            "temperature_c": raw_payload.get("temperature_c"),
            "humidity_percent": raw_payload.get("humidity_percent"),
            "motion_detected": raw_payload.get("motion_detected"),
            "co2_ppm": raw_payload.get("co2_ppm"),
            "smoke_ppm": raw_payload.get("smoke_ppm"),
            "battery_percent": raw_payload.get("battery_percent"),
            "status": analysis["status"],
            "alert_level": analysis["alert_level"],
            "reason": analysis["reason"]
        }
        client.publish(TOPIC_PROCESSED_OUTPUT, json.dumps(processed_event))
    except Exception as e:
        print(f"❌ Lỗi MQTT thread: {e}")

mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message

def start_mqtt_loop():
    try:
        mqtt_client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, 60)
        mqtt_client.loop_forever()
    except Exception:
        pass

threading.Thread(target=start_mqtt_loop, daemon=True).start()

# --- 6. HỆ THỐNG ENDPOINTS HTTP REST CHO SWAGGER UI ---
class HealthResponse(BaseModel):
    status: str
    service: str
    version: str

@app.get("/health", response_model=HealthResponse)
def get_health():
    return {"status": "ok", "service": SERVICE_NAME, "version": SERVICE_VERSION}

@app.post("/readings", response_model=SensorReadingCreated, status_code=status.HTTP_201_CREATED, dependencies=[Depends(verify_bearer_token)])
def create_reading(payload: SensorReadingCreate):
    reading_id = f"READ-{len(READINGS) + 1:03d}"
    current_time = datetime.now(timezone.utc).isoformat()
    dev_info = DEVICE_REGISTRY.get(payload.device_id.strip().lower(), {})
    
    response_data = {
        "reading_id": reading_id,
        "device_id": payload.device_id,
        "metric": payload.metric,
        "accepted": True,
        "created_at": current_time,
        "device_type": dev_info.get("device_type", "unknown"),
        "location": dev_info.get("location", "unknown"),
        "room": dev_info.get("room", "unknown"),
        "device_status": dev_info.get("status", "unknown")
    }
    # --- BỔ SUNG: Đóng gói schema sự kiện và phát sang máy bạn Minh ---
    try:
        processed_event = {
            "event_type": "sensor.reading.processed",
            "source_service": "team-iot",
            "raw_event_id": f"raw-swagger-{reading_id}",
            "device_id": payload.device_id,
            "location": dev_info.get("location", "unknown"),
            "temperature_c": payload.value if payload.metric == SensorMetric.temperature else 25.0,
            "humidity_percent": payload.value if payload.metric == SensorMetric.humidity else 50.0,
            "motion_detected": False,
            "co2_ppm": 400,
            "smoke_ppm": 0.0,
            "battery_percent": 100,
            "status": "normal",            # Gán thẳng để test kết nối nhanh
            "alert_level": "low",          # Gán thẳng để test kết nối nhanh
            "reason": "swagger_test_trigger"
        }
        # Thực hiện bắn dữ liệu trực tiếp sang IP của Minh B6
        mqtt_client.publish(TOPIC_PROCESSED_OUTPUT, json.dumps(processed_event))
        print(f"📤 [Swagger Trigger] Đã đẩy dữ liệu sạch lên topic: {TOPIC_PROCESSED_OUTPUT}")
    except Exception as e:
        print(f"❌ Lỗi bắn MQTT từ Swagger: {e}")

    READINGS.append(response_data)
    return response_data    

@app.get("/readings/latest", dependencies=[Depends(verify_bearer_token)])
def get_latest_readings(device_id: str = Query(...), limit: int = Query(5)):
    items = [r for r in READINGS if r["device_id"] == device_id]
    return {"items": items[:limit]}
