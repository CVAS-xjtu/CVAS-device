import numpy as np
import matplotlib.pyplot as plt

class EKF_GPS_IMU_Back:
    """
    状态向量 (16维):
        [px, py, pz, vx, vy, vz, qw, qx, qy, qz, ba_x, ba_y, ba_z, bg_x, bg_y, bg_z]
    适用: 背部佩戴 IMU + GPS，室外行走，无 ZUPT
    观测: GPS 位置 (3维) + 磁力计航向 (1维)
    """
    def __init__(self, dt, g=np.array([0, 0, -9.81])):
        self.dt = dt
        self.g = g
        
        # 状态初始化
        self.x = np.zeros(16)
        self.x[6] = 1.0                   
        self.P = np.eye(16) * 0.1
        self.P[6:10, 6:10] = np.eye(4) * 0.01
        
        # 过程噪声协方差
        self.Q = np.eye(16) * 0.01
        self.Q[0:3, 0:3] = np.eye(3) * 0.02
        self.Q[3:6, 3:6] = np.eye(3) * 0.2
        self.Q[6:10, 6:10] = np.eye(4) * 0.0005
        self.Q[10:13, 10:13] = np.eye(3) * 0.001
        self.Q[13:16, 13:16] = np.eye(3) * 0.001
        
        # 观测噪声协方差
        self.R_gps = np.diag([1.0, 1.0, 8.0])   # 水平1m，垂直8m
        self.R_yaw = 0.02                       # 磁航向标准差 ~8°
        
        # 雅可比占位
        self.F = np.eye(16)
        self.H_gps = np.zeros((3, 16))
        self.H_gps[:, 0:3] = np.eye(3)
    
    # 四元数运算辅助
    def quat_multiply(self, q, r):
        w1, x1, y1, z1 = q; w2, x2, y2, z2 = r
        return np.array([
            w1*w2 - x1*x2 - y1*y2 - z1*z2,
            w1*x2 + x1*w2 + y1*z2 - z1*y2,
            w1*y2 - x1*z2 + y1*w2 + z1*x2,
            w1*z2 + x1*y2 - y1*x2 + z1*w2
        ])
    
    def quat_conjugate(self, q):
        return np.array([q[0], -q[1], -q[2], -q[3]])
    
    def quat_rotate(self, q, v):
        q_vec = q[1:]; q_w = q[0]
        return v + 2 * np.cross(q_vec, np.cross(q_vec, v) + q_w * v)
    
    def quat_from_axis_angle(self, axis, angle):
        half = angle / 2.0
        return np.array([np.cos(half), axis[0]*np.sin(half),
                         axis[1]*np.sin(half), axis[2]*np.sin(half)])
    
    # 过程方程 f(x, u)
    def state_transition(self, x, u):
        dt = self.dt
        p = x[0:3]; v = x[3:6]; q = x[6:10]; ba = x[10:13]; bg = x[13:16]
        gyro = u[0:3]; acc = u[3:6]
        
        gyro_c = gyro - bg
        acc_c = acc - ba
        
        # 姿态更新
        axis = gyro_c / (np.linalg.norm(gyro_c) + 1e-8)
        angle = np.linalg.norm(gyro_c) * dt
        dq = self.quat_from_axis_angle(axis, angle)
        q_new = self.quat_multiply(q, dq)
        q_new = q_new / np.linalg.norm(q_new)
        
        # 速度更新
        a_world = self.quat_rotate(q, acc_c) + self.g
        v_new = v + a_world * dt
        
        # 位置更新
        p_new = p + v * dt + 0.5 * a_world * dt**2
        
        return np.hstack([p_new, v_new, q_new, ba, bg])
    
    # 数值计算雅可比
    def compute_F(self, x, u):
        n = 16; eps = 1e-6; F = np.zeros((n, n))
        for i in range(n):
            x_plus = x.copy(); x_plus[i] += eps
            x_minus = x.copy(); x_minus[i] -= eps
            F[:, i] = (self.state_transition(x_plus, u) -
                       self.state_transition(x_minus, u)) / (2 * eps)
        return F
    
    # 观测模型
    def observation_position(self, x):
        return x[0:3]
    
    def observation_yaw(self, x):
        qw, qx, qy, qz = x[6:10]
        return np.arctan2(2 * (qw * qz + qx * qy), 1 - 2 * (qy**2 + qz**2))
    
    # 磁力计预处理
    def compute_yaw_from_mag(self, acc, mag):
        ax, ay, az = acc
        pitch = np.arcsin(np.clip(-ax / 9.81, -1, 1))
        roll = np.arctan2(ay, -az)
        mx, my, mz = mag
        my1 = my * np.cos(-roll) - mz * np.sin(-roll)
        mz1 = my * np.sin(-roll) + mz * np.cos(-roll)
        mx2 = mx * np.cos(-pitch) + mz1 * np.sin(-pitch)
        return np.arctan2(-my1, mx2)
    
    # EKF 预测
    def predict(self, u):
        x_prev = self.x.copy()
        self.F = self.compute_F(x_prev, u)
        self.x = self.state_transition(x_prev, u)
        self.P = self.F @ self.P @ self.F.T + self.Q
    
    # EKF 更新: GPS 位置
    def update_gps(self, z_pos):
        H = self.H_gps
        z_hat = self.observation_position(self.x)
        y = z_pos - z_hat
        S = H @ self.P @ H.T + self.R_gps
        K = self.P @ H.T @ np.linalg.inv(S)
        self.x = self.x + K @ y
        self.P = (np.eye(16) - K @ H) @ self.P
        self.x[6:10] = self.x[6:10] / np.linalg.norm(self.x[6:10])
    
    # EKF 更新: 磁力计航向
    def update_yaw(self, yaw_meas):
        yaw_pred = self.observation_yaw(self.x)
        y = yaw_meas - yaw_pred
        y = np.arctan2(np.sin(y), np.cos(y))   # 角度归一化
        
        eps = 1e-6
        H = np.zeros((1, 16))
        for i in range(6, 10):
            x_plus = self.x.copy(); x_plus[i] += eps
            x_minus = self.x.copy(); x_minus[i] -= eps
            H[0, i] = (self.observation_yaw(x_plus) -
                       self.observation_yaw(x_minus)) / (2 * eps)
        
        S = H @ self.P @ H.T + self.R_yaw
        K = self.P @ H.T / S[0, 0]
        self.x = self.x + K.flatten() * y
        self.P = (np.eye(16) - K @ H) @ self.P
        self.x[6:10] = self.x[6:10] / np.linalg.norm(self.x[6:10])
    
    # 获取位置
    def get_position(self):
        return self.x[0:3].copy()


