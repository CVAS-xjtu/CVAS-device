import numpy as np
import time

from imu import IMU类名   
from gps import GPS类名   

from fusion import EKF_GPS_IMU_Back  

def main():
   
    imu_sensor = IMU类名()  
    gps_sensor = GPS类名()
    
    dt_imu = 0.01  # 根据IMU实际采样间隔修改
    ekf = EKF_GPS_IMU_Back(dt=dt_imu)
    
    # 设置初始位置
    ekf.x[0:3] = np.array([0.0, 0.0, 0.0])  
    ekf.x[3:6] = np.array([0.0, 0.0, 0.0])   # 初始速度
    ekf.x[6:10] = np.array([1.0, 0.0, 0.0, 0.0])  # 单位四元数

    print("定位开始...")
    frame = 0
    
    try:
        while True:
            # 读取 IMU 数据（替换为你的实际接口）
            # 假设 imu 文件里有 read_imu() 方法，返回 (gyro, acc, mag)
            # 若返回的是列表，转为 numpy 数组
            gyro, acc, mag = imu_sensor.读取方法()  
            
            # 组装输入 [wx, wy, wz, ax, ay, az]
            u = np.hstack([np.array(gyro), np.array(acc)])
            
            # EKF 预测
            ekf.predict(u)
            
            # 读取 GPS 数据（替换为实际接口）
            # 假设 gps 文件里有read_gps()方法，返回 (经度, 纬度, 高度)
    
            lat, lon, alt = gps_sensor.读取方法()  
            # 例如: lat, lon, alt = gps_sensor.get_gps_llh()
            # 将经纬高转为 ENU（如果已经有转换函数调用，没有则需实现）
            # 这里假设 GPS 直接输出 ENU 坐标 [x, y, z]，如果不是，需要坐标转换
            gps_enu = np.array([x, y, z])  # 请替换为实际的转换值
            
            # 如果有有效 GPS 数据，执行更新
            if gps_enu is not None:
                ekf.update_gps(gps_enu)
            
            # 磁力计航向更新（降频)
            if frame % 50 == 0:  # 假设 dt=0.01，50帧=0.5秒
                yaw = ekf.compute_yaw_from_mag(np.array(acc), np.array(mag))
                ekf.update_yaw(yaw)
            
            #  输出结果
            pos = ekf.get_position()
            print(f"位置: X={pos[0]:.2f}, Y={pos[1]:.2f}, Z={pos[2]:.2f}")
            
            frame += 1
            time.sleep(dt_imu)  # 模拟循环间隔，实际可去掉
            
    except KeyboardInterrupt:
        print("程序退出。")

if __name__ == "__main__":
    main()