# 索引
- [drivers/](#drivers)
    - [函数与全局变量](#函数与全局变量)
    - [GPIO](#gpio)
    - [StereoCamera](#stereocamera)
    - [BluetoothManager](#bluetoothmanager)

# [drivers/](./src/drivers/)

## [函数与全局变量:](./src/drivers/env_check.py)
`is_real_jetson()`  
函数，真实Jetson硬件时返回True

`is_sim_env()`  
函数，PC仿真时返回True

`SIM_LOG`  
值为False时取消打印仿真日志，默认打印

## [GPIO:](./src/drivers/gpio.py)
*静态类，使用时例如 `GPIO.input(3)`*

`setmode(cls, mode)`  
设置引脚编号模式为BOARD/BCM

`setup(cls, pin, mode, initial=None)`  
初始化引脚，pin为引脚号，mode为输入输出模式IN/OUT，initial为初始化电平HIGH/LOW

`output(cls, pin, value)`  
输出pin引脚电平，value为HIHG/LOW

`input(cls, pin)`  
读取pin引脚电平，返回值是HIGH/LOW

`cleanup(cls, pin=None)`  
没有pin时释放所有引脚，有pin时释放指定引脚

## [StereoCamera:](./src/drivers/camera.py)
*创建实例时一次性创建两个相机，需要传入camera字典*

`open(self)`  
启动左右相机采集线程

`stop(self)`  
停止左右相机采集线程

`read(self)`  
读取左右相机帧，返回值是left_frame, right_frame

## [BluetoothManager:](./src/drivers/bluetooth.py)
*创建实例时自动开启子进程和所有线程，需要传入audio字典*

`disconnect(self)`  
主动断开耳机连接

`connect(self)`  
主动连接耳机，成功返回True，失败返回False

`stop(self)`  
关闭所有蓝牙子进程和线程

`get_target_mac(self)`  
返回值是目标蓝牙MAC地址

`is_connected(self, force_refresh: bool = False)`  
查询蓝牙连接状态，连接返回True，未连接返回False，force_refresh=True时强制刷新读取连接状态

`get_battery_level(self, force_refresh: bool = False)`  
读取电池电量百分比，返回值是不带百分号的整数，没有时返回None，force_refresh=True时强制刷新读取电量

