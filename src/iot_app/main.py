import http
import os
import csv
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from fastapi.middleware.cors import CORSMiddleware

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Đọc biến môi trường với giá trị mặc định
SERVICE_NAME = os.getenv("SERVICE_NAME", "iot-ingestion")
SERVICE_VERSION = os.getenv("SERVICE_VERSION", "0.5.0")
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "local-dev-token")


app = FastAPI(
    title="FIT4110 Lab 05 - IoT Ingestion Service",
    version=SERVICE_VERSION,
    description=(
        "IoT Ingestion API chạy trong ngữ cảnh Docker Compose cho Lab 05. "
        "Luồng logic được kế thừa từ Lab 04 và tiếp tục được dùng để kiểm thử end‑to‑end."
    ),
)
# --- BỔ SUNG: Cấu hình CORS cho phép gọi API từ mọi thiết bị, mọi trình duyệt ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],             # Cho phép tất cả các nguồn truy cập
    allow_credentials=True,
    allow_methods=["*"],             # Cho phép tất cả các phương thức GET, POST, PUT, DELETE
    allow_headers=["*"],             # Cho phép tất cả các Headers (bao gồm cả Authorization)
)

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


class ProblemDetails(BaseModel):
    type: str = "about:blank"
    title: str
    status: int = Field(..., ge=400, le=599)
    detail: str
    instance: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str


class SensorReadingCreate(BaseModel):
    device_id: str = Field(..., min_length=3, examples=["ESP32-LAB-A01"])
    metric: SensorMetric = Field(..., examples=["temperature"])
    value: float = Field(
        ...,
        ge=-40,
        le=80,
        description="Boundary range used in Lab 03 và Lab 04: -40 đến 80.",
        examples=[31.5],
    )
    unit: Optional[SensorUnit] = Field(default=None, examples=["celsius"])
    timestamp: str = Field(..., examples=["2026-05-13T08:30:00+07:00"])


class SensorReading(BaseModel):
    reading_id: str
    device_id: str
    metric: SensorMetric
    value: float
    unit: Optional[SensorUnit] = None
    timestamp: str
    created_at: str


class SensorReadingCreated(BaseModel):
    reading_id: str
    device_id: str
    metric: SensorMetric
    accepted: bool
    created_at: str
    # --- BỔ SUNG: Thêm các trường để trả về thông tin từ CSV ---
    device_type: Optional[str] = Field(default=None, examples=["environment_sensor"])
    location: Optional[str] = Field(default=None, examples=["Lab A101"])
    room: Optional[str] = Field(default=None, examples=["A101"])
    device_status: Optional[str] = Field(default=None, examples=["active"])



READINGS: List[Dict] = []
# --- BỔ SUNG: Logic nạp danh sách thiết bị hợp lệ ---
DEVICE_REGISTRY: Dict[str, Dict] = {}

# Đường dẫn động tìm file iot_device_registry.csv nằm ở thư mục gốc dự án
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
                    # Thêm .strip().lower() vào cuối dev_id để làm sạch chuỗi
                    DEVICE_REGISTRY[dev_id.strip().lower()] = {
                        "device_type": row.get("device_type"),
                        "location": row.get("location"),
                        "room": row.get("room"),
                        "status": row.get("status")
                    }
        print(f"✅ Đã nạp thành công {len(DEVICE_REGISTRY)} thiết bị vào bộ nhớ.")
    except Exception as e:
        print(f"❌ Lỗi khi đọc file registry: {e}")

# Gọi hàm nạp dữ liệu ngay khi khởi chạy ứng dụng
load_device_registry()
# --------------------------------------------------

def build_problem(
    *,
    status_code: int,
    title: str,
    detail: str,
    instance: Optional[str] = None,
    problem_type: str = "about:blank",
) -> Dict:
    problem = {
        "type": problem_type,
        "title": title,
        "status": status_code,
        "detail": detail,
    }
    if instance:
        problem["instance"] = instance
    return problem


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict):
        problem = exc.detail
    else:
        # Sửa lỗi: Sử dụng thư viện http tiêu chuẩn thay vì status.HTTP_STATUS_CODES lỗi thời
        try:
            phrase = http.HTTPStatus(exc.status_code).phrase
        except ValueError:
            phrase = "HTTP Error"

        problem = build_problem(
            status_code=exc.status_code,
            title=phrase,
            detail=str(exc.detail),
            instance=str(request.url.path),
        )

    try:
        default_phrase = http.HTTPStatus(exc.status_code).phrase
    except ValueError:
        default_phrase = "HTTP Error"

    problem.setdefault("status", exc.status_code)
    problem.setdefault("title", default_phrase)
    problem.setdefault("type", "about:blank")
    problem.setdefault("detail", str(exc.detail) if exc.detail else "Request failed")
    problem.setdefault("instance", str(request.url.path))

    return JSONResponse(
        status_code=exc.status_code,
        content=problem,
        media_type="application/problem+json",
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    first_error = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(item) for item in first_error.get("loc", []))
    message = first_error.get("msg", "Request validation error")
    detail = f"{location}: {message}" if location else message

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=build_problem(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            title="Validation error",
            detail=detail,
            instance=str(request.url.path),
            problem_type="https://smart-campus.local/problems/validation-error",
        ),
        media_type="application/problem+json",
    )


from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

# Khởi tạo Security Scheme theo chuẩn HTTP Bearer
security = HTTPBearer()

def verify_bearer_token(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)) -> None:
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=build_problem(
                status_code=status.HTTP_401_UNAUTHORIZED,
                title="Unauthorized",
                detail="Missing Authorization header",
                instance="/readings",
                problem_type="https://smart-campus.local",
            ),
        )

    # Lấy chuỗi token nguyên bản đã được bóc tách tự động (bỏ qua chữ "Bearer ")
    token = credentials.credentials
    if token != AUTH_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=build_problem(
                status_code=status.HTTP_401_UNAUTHORIZED,
                title="Unauthorized",
                detail="Invalid bearer token",
                instance="/readings",
                problem_type="https://smart-campus.local",
            ),
        )



def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def next_reading_id() -> str:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"R-{today}-{len(READINGS) + 1:04d}"


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service=SERVICE_NAME,
        version=SERVICE_VERSION,
    )


@app.post(
    "/readings",
    response_model=SensorReadingCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_bearer_token)],
    responses={
        401: {"model": ProblemDetails},
        422: {"model": ProblemDetails},
        429: {"model": ProblemDetails},
    },
)
def create_reading(payload: SensorReadingCreate, response: Response) -> SensorReadingCreated:
    # 1. Khởi tạo các thuộc tính trạng thái mặc định
    env_status = "normal"
    alert_level = "none"
    reason = "environment_normal"

    # 2. Bước Validate: Kiểm tra thiết bị trong Device Registry (File CSV)
    # LƯU Ý: Chuyển payload.device_id sang chữ thường (lowercase) để khớp chính xác với file CSV của bạn
    device_key = payload.device_id.strip().lower()
    if device_key not in DEVICE_REGISTRY:
        env_status = "invalid_device"
        alert_level = "high"
        reason = "device_not_registered"
        
        # Trả về lỗi 400 Bad Request ngay lập tức nếu thiết bị lạ
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=build_problem(
                status_code=status.HTTP_400_BAD_REQUEST,
                title="Invalid Device",
                detail=f"Device '{payload.device_id}' không tồn tại trong hệ thống đăng ký.",
                instance="/readings",
                problem_type="https://smart-campus.local"
            )
        )

    # 3. Bước Validate & Phân loại: Kiểm tra giá trị lỗi cảm biến (Sensor Error)
    if payload.value is None:
        env_status = "sensor_error"
        alert_level = "high"
        reason = "sensor_values_null"

    # 4. Bước Phân loại: Áp dụng Rules Engine đánh giá trạng thái môi trường
    else:
        val = payload.value
        metric = payload.metric

        # --- Kiểm tra mức NGUY HIỂM (Danger) ---
        if (metric == SensorMetric.temperature and val >= 40) or \
           (metric == "co2" and val >= 1800) or \
           (metric == SensorMetric.smoke and val >= 1.0):
            env_status = "danger"
            alert_level = "critical"
            if metric == SensorMetric.temperature: reason = "extreme_high_temperature"
            elif metric == "co2": reason = "extreme_high_co2"
            else: reason = "smoke_detected_danger"

        # --- Kiểm tra mức CẢNH BÁO (Warning) ---
        elif (metric == SensorMetric.temperature and val >= 35) or \
             (metric == SensorMetric.humidity and val >= 85) or \
             (metric == "co2" and val >= 1200) or \
             (metric == SensorMetric.smoke and val >= 0.5) or \
             (metric == "battery" and val < 20):
            env_status = "warning"
            alert_level = "medium"
            if metric == SensorMetric.temperature: reason = "high_temperature_warning"
            elif metric == SensorMetric.humidity: reason = "high_humidity_warning"
            elif metric == "co2": reason = "high_co2_warning"
            elif metric == SensorMetric.smoke: reason = "smoke_detected_warning"
            else: reason = "low_battery_warning"

    # 5. Thêm Header thông báo nếu rơi vào trạng thái nguy hiểm hoặc cảnh báo
    if env_status in ["warning", "danger"]:
        response.headers["X-Warning"] = f"{env_status}-{payload.metric.value}"

    # 6. Tạo gói dữ liệu sạch (Processed Event)
    reading_id = next_reading_id()
    created_at = now_iso()

    device_info = DEVICE_REGISTRY.get(device_key, {})
    item = {
        "reading_id": reading_id,
        "device_id": payload.device_id,
        "metric": payload.metric.value,
        "value": payload.value,
        "unit": payload.unit.value if payload.unit else None,
        "timestamp": payload.timestamp,
        "created_at": created_at,
        "status": env_status,
        "alert_level": alert_level,
        "reason": reason,
        # Lưu kèm thông tin thiết bị vào mảng READINGS nội bộ
        "device_type": device_info.get("device_type"),
        "location": device_info.get("location"),
        "room": device_info.get("room"),
        "device_status": device_info.get("status")
    }
    READINGS.append(item)

    return SensorReadingCreated(
        reading_id=reading_id,
        device_id=payload.device_id,
        metric=payload.metric,
        accepted=True,
        created_at=created_at,
        device_type=device_info.get("device_type"),
        location=device_info.get("location"),
        room=device_info.get("room"),
        device_status=device_info.get("status") # Tên cột status trong file CSV của bạn
    )

@app.get("/readings/latest", dependencies=[Depends(verify_bearer_token)])
def latest_readings(
    device_id: Optional[str] = Query(default=None),
    limit: int = Query(default=10, ge=1, le=100),
) -> Dict[str, List[Dict]]:
    items = READINGS

    if device_id:
        items = [item for item in items if item["device_id"] == device_id]

    return {"items": items[-limit:]}


@app.get("/readings/{reading_id}", dependencies=[Depends(verify_bearer_token)])
def get_reading(reading_id: str) -> Dict:
    for item in READINGS:
        if item["reading_id"] == reading_id:
            return item

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=build_problem(
            status_code=status.HTTP_404_NOT_FOUND,
            title="Not Found",
            detail=f"Reading {reading_id} does not exist",
            instance=f"/readings/{reading_id}",
            problem_type="https://smart-campus.local/problems/not-found",
        ),
    )
if __name__ == "__main__":
    import uvicorn
    # Đọc cấu hình APP_HOST và APP_PORT từ file .env qua môi trường hệ thống
    host = os.getenv("APP_HOST", "0.0.0.0")
    port = int(os.getenv("APP_PORT", "8000"))
    
    # Ép buộc uvicorn khởi chạy đúng theo cấu hình biến môi trường
    uvicorn.run("main:app", host=host, port=port, reload=True)