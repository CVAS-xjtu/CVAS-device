    # def scan_nearby_devices(self, scan_duration: float = 4.0) -> dict[str, str]:
    #     """
    #     扫描周边蓝牙设备
    #     return: {设备名称: MAC地址}
    #     """
    #     # 开启扫描
    #     self._send_command("scan on")
    #     time.sleep(scan_duration)
    #     # 一次性读取所有已发现设备
    #     result = subprocess.check_output(
    #         ["bluetoothctl", "devices"],
    #         text=True
    #     )
    #     dev_dict = {}
    #     for line in result.splitlines():
    #         parts = line.strip().split()
    #         if len(parts) >= 3:
    #             mac_addr = parts[1]
    #             dev_name = " ".join(parts[2:])
    #             dev_dict[dev_name] = mac_addr
    #     # 可选：关闭扫描节省功耗
    #     # self._send_command("scan off")
    #     return dev_dict
    
    # def set_target_mac(self, new_mac: str):
    #     with self._state_lock:
    #         self._target_mac = new_mac.strip().upper()
    #         # 修改MAC后清空旧声卡状态
    #         self._clear_audio_id_cache()
    #         # 持久化到yaml
    #         try:
    #             self._save_target_mac()
    #         except Exception:
    #             pass


    # def _save_target_mac(self):
    #     data = {"target_mac": self._target_mac}
    #     with open(self._cfg_file, "w", encoding="utf-8") as f:
    #         yaml.safe_dump(data, f)


    # def _load_target_mac(self):
    #     try:
    #         config_file = self._cfg_file
    #         if not config_file.exists() and self._legacy_cfg_file.exists():
    #             config_file = self._legacy_cfg_file

    #         if config_file.exists():
    #             with open(config_file, "r", encoding="utf-8") as f:
    #                 data = yaml.safe_load(f) or {}
    #                 mac = data.get("target_mac")
    #                 if mac:
    #                     self._target_mac = str(mac).strip().upper()
    #                     if config_file is self._legacy_cfg_file:
    #                         # 如果旧文件存在，迁移到新配置文件
    #                         self._save_target_mac()
    #     except Exception:
    #         pass



    # def pair_and_connect(self):
    #     """核心：复刻手动配对→信任→连接整套流程，加锁防并发"""
    #     with self._op_lock:
    #         if self._check_connected():
    #             return
    #         # 发起配对
    #         self._send_command(f"pair {self._target_mac}")
    #         time.sleep(2.2)
    #         # 信任设备，避免后续弹窗确认
    #         self._send_command(f"trust {self._target_mac}")
    #         time.sleep(1.0)
    #         # 建立音频连接
    #         self._send_command(f"connect {self._target_mac}")
    #         time.sleep(1.8)