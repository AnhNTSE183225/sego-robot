#!/usr/bin/env python3
from rplidar import RPLidar, RPLidarException
import time

LIDAR_PORT = "/dev/ttyUSB0"  # đổi nếu bạn đang dùng port khác
MAX_SCANS = 10               # số vòng quét để lấy mẫu (10 là đủ)

def main():
    print("=== LIDAR Direction Probe ===")
    print(f"Using port: {LIDAR_PORT}")
    print("1) Đặt vật cản ở VỊ TRÍ BẠN MUỐN TEST (ví dụ: bên trái robot, cách 0.3–0.8m)")
    print("2) Đảm bảo xung quanh không quá nhiều vật cản gần hơn vật test.")
    input("Nhấn Enter để bắt đầu đo...")

    try:
        lidar = RPLidar(LIDAR_PORT)
    except RPLidarException as e:
        print(f"[ERROR] Không mở được LIDAR trên {LIDAR_PORT}: {e}")
        return

    try:
        info = lidar.get_info()
        health = lidar.get_health()
        print(f"LIDAR info: {info}")
        print(f"LIDAR health: {health}")
    except Exception as e:
        print(f"[WARN] Không đọc được info/health: {e}")

    print("Đang thu thập dữ liệu scan...")
    samples = []

    try:
        # iter_scans trả về list các điểm: (quality, angle_deg, distance_mm)
        for i, scan in enumerate(lidar.iter_scans()):
            samples.extend(scan)
            if i + 1 >= MAX_SCANS:
                break
            time.sleep(0.01)
    except RPLidarException as e:
        print(f"[ERROR] Lỗi khi đọc scan: {e}")
    finally:
        try:
            lidar.stop()
            lidar.stop_motor()
            lidar.disconnect()
        except Exception:
            pass

    if not samples:
        print("[ERROR] Không thu được điểm LIDAR nào.")
        return

    # Lọc các điểm có khoảng cách hợp lý (10cm–3m) để tránh nhiễu
    filtered = [
        (q, ang, dist)
        for (q, ang, dist) in samples
        if dist > 100 and dist < 3000  # 0.1m – 3m
    ]

    if not filtered:
        print("[WARN] Không có điểm nào trong khoảng 0.1–3m. Thử đặt vật gần hơn và chạy lại.")
        return

    # Tìm điểm gần nhất
    closest = min(filtered, key=lambda s: s[2])
    q, angle_deg, dist_mm = closest
    dist_m = dist_mm / 1000.0

    print("\n=== KẾT QUẢ ===")
    print(f"Số điểm thu được: {len(samples)}, sau lọc: {len(filtered)}")
    print(f"Điểm gần nhất:")
    print(f"  angle (raw)  = {angle_deg:.1f}°")
    print(f"  distance     = {dist_m:.3f} m")
    print(f"  quality      = {q}")
    print("\nGửi lại cho mình góc `angle (raw)` này,")
    print("và mô tả: lúc đo, vật nằm ở đâu (trước / trái / phải robot).")

if __name__ == "__main__":
    main()
