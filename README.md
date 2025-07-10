# bilibili-cheese-downloader 哔哩哔哩课程下载器
## 哔哩哔哩课程下载器 / bilibili cheeses downloader 下载你已购买的或免费的 bilibili 课程。  
### 使用方式：
1. 下载 bdownloader.py 或 release 中的 exe 文件后运行即可。
2. 手机 app 扫码完成登录，随后输入你已拥有的课程 ID 即可完成下载操作。  
注意：通过 ffmpeg 进行音视频合成操作，所以请提前配置好环境变量或将其复制到主程序同级目录下。  
 
### 所需依赖：
python==3.9  
pip install tqdm bilibili-api-python aiohttp 
基本是 ai 搓的，所以问题有很多，有空再改改。
点个 star 谢谢喵，爱你喵。

### 更新：
bdownloader_3.0.py 更新了帧率转换，编码转换，能极大压缩文件体积大小；同时优化了日志，增强了代码健壮性。（编码转换耗时较长，请考虑充分）