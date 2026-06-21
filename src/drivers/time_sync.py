import time
from collections import deque
from typing import Dict, Tuple, Optional, Any, List

class TimeSyncMatcher:
    """
    通用多传感器时间戳匹配器
    支持两路数据流按时间差阈值配对（相机左/右、IMU/图像、GPS/图像通用）
    """
    def __init__(self, max_cache_ms: float = 100, match_threshold_ms: float = 8):
        """
        :param max_cache_ms: 缓存最长保留时长，超过自动丢弃旧数据
        :param match_threshold_ms: 匹配允许最大时间差
        """
        self.max_cache_sec = max_cache_ms / 1000.0
        self.match_thresh_sec = match_threshold_ms / 1000.0

        # 两路缓存队列：每个元素 (timestamp, data)
        self.queue_a = deque()
        self.queue_b = deque()

    def add_data_a(self, data: Any, ts: float = None):
        """添加A路数据（左相机/图像），不传ts自动取当前系统时间"""
        if ts is None:
            ts = time.time()
        self.queue_a.append((ts, data))
        self._prune_expired()

    def add_data_b(self, data: Any, ts: float = None):
        """添加B路数据（右相机/IMU/GPS）"""
        if ts is None:
            ts = time.time()
        self.queue_b.append((ts, data))
        self._prune_expired()

    def _prune_expired(self):
        """清理超出缓存时长的过期数据，防止内存堆积"""
        now = time.time()
        # 清理A队列旧数据
        while self.queue_a and now - self.queue_a[0][0] > self.max_cache_sec:
            self.queue_a.popleft()
        # 清理B队列旧数据
        while self.queue_b and now - self.queue_b[0][0] > self.max_cache_sec:
            self.queue_b.popleft()

    def find_best_match(self) -> Optional[Tuple[Any, Any]]:
        """
        查找时间差最小、小于阈值的一对数据
        返回 (data_a, data_b)，无匹配返回None
        """
        best_pair = None
        min_diff = float("inf")

        # 暴力遍历简单高效，缓存很短不会卡
        for ts_a, data_a in self.queue_a:
            for ts_b, data_b in self.queue_b:
                diff = abs(ts_a - ts_b)
                if diff < self.match_thresh_sec and diff < min_diff:
                    min_diff = diff
                    best_pair = (data_a, data_b)
        return best_pair

    def clear(self):
        """清空两路缓存，切换场景/释放时调用"""
        self.queue_a.clear()
        self.queue_b.clear()





class MultiTimeSyncMatcher:
    """
    支持N路传感器统一时间戳匹配
    可同时管理：左相机、右相机、IMU、GPS、雷达等多路数据
    """
    def __init__(self, max_cache_ms=100, match_threshold_ms=8):
        self.max_cache_sec = max_cache_ms / 1000.0
        self.thresh_sec = match_threshold_ms / 1000.0
        # 多路缓存 {sensor_name: deque[(ts, data)]}
        self.queues: Dict[str, deque] = {}

    def register_sensor(self, sensor_name: str):
        """注册传感器，提前创建队列"""
        if sensor_name not in self.queues:
            self.queues[sensor_name] = deque()

    def add_data(self, sensor_name: str, data: Any, ts: float = None):
        """向指定传感器队列添加数据，自动清理过期帧"""
        if ts is None:
            ts = time.time()
        # 不存在则自动注册
        if sensor_name not in self.queues:
            self.register_sensor(sensor_name)
        self.queues[sensor_name].append((ts, data))
        self._prune_expired()

    def _prune_expired(self):
        """全局清理所有队列过期数据"""
        now = time.time()
        for q in self.queues.values():
            while q and now - q[0][0] > self.max_cache_sec:
                q.popleft()

    def get_fully_matched_group(self, sensor_list: List[str]) -> Optional[Dict[str, Any]]:
        """
        传入需要全部对齐的传感器列表，返回一套时间匹配的完整数据
        返回 {sensor_name: data}，缺少任意一路匹配则返回None
        """
        # 收集所有传感器全部(ts, data)
        all_groups = []
        for name in sensor_list:
            if name not in self.queues or len(self.queues[name]) == 0:
                return None
            all_groups.append(list(self.queues[name]))

        best_set = None
        min_total_diff = float("inf")

        # 多路组合遍历，寻找整体时间最接近的一组
        # 传感器数量少（≤4）时性能完全无压力
        from itertools import product
        for items in product(*all_groups):
            timestamps = [item[0] for item in items]
            datas = [item[1] for item in items]
            # 最大时间差作为这套数据的误差
            span = max(timestamps) - min(timestamps)
            if span < self.thresh_sec and span < min_total_diff:
                min_total_diff = span
                best_set = dict(zip(sensor_list, datas))

        return best_set

    def clear_all(self):
        """清空全部传感器缓存"""
        for q in self.queues.values():
            q.clear()