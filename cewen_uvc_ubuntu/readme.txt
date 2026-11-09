安装依赖
sudo apt update
sudo apt install build-essential libusb-1.0-0-dev libjpeg-dev cmake git

安装libuvc
git clone https://github.com/libuvc/libuvc.git
cd libuvc
mkdir build
cd build
cmake ..
make
sudo make install
sudo ldconfig

编译
make

运行（同时打开热成像摄像头和 IMX462，并开始录制）
python img.py

如果更新过 C 代码（uvc_camera.c / hik_sensor.c / hik_capture.c），
必须先重新 make，再运行 python img.py。

第一次运行会提示输入 sudo 密码，输入并确认后才会启动热成像；
如果不输入密码，热成像不会出画面。

录制文件保存在 ./recordings/，热成像为 thermal_*.mp4，CSI 为 csi_*.mp4。
注意：在这台 Orin Nano 上，IMX462 被识别为 USB FHD Camera（/dev/video2），
不是 CSI。img.py 会先自动查找 USB/V4L2 摄像头；找不到时再尝试 CSI 的
1920x1080@30 和 1280x720@60（即 test_camera_csi.py 使用的管道）。
如果自动识别不到，可以手动指定：
python img.py --v4l2-device /dev/video2

无显示器运行：python img.py --no-display
手动指定 CSI 参数：python img.py --sensor-id 0 --csi-width 1280 --csi-height 720 --csi-fps 60

温度超过 40°C 时，会自动把热成像和 IMX462 两张照片同时保存到
./captured_images/。

由于热成像图像可能存在偏移，可以修改 img.py 里的 SHIFT_X / SHIFT_Y。
