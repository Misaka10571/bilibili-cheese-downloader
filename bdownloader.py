import os
import subprocess
import time
import json
import re
import uuid
from tqdm import tqdm
import threading
import qrcode
from tkinter import Tk, Canvas, PhotoImage
import asyncio

# 导入bilibili-api库
from bilibili_api import Credential, cheese, video, sync

# 创建下载目录
if not os.path.exists('./download/temp'):
    os.makedirs('./download/temp')

# 解析时间为秒
def parse_time_2_sec(s):
    duration_match = re.search(r'(\d{2}):(\d{2}):(\d{2})', s)
    if not duration_match:
        return 0
    hours, minutes, seconds_milliseconds = duration_match.groups()
    seconds = int(hours) * 3600 + int(minutes) * 60
    seconds += int(seconds_milliseconds)
    return seconds

# 下载文件
async def download_file(url, save_path, desc):
    # 发送GET请求获取文件大小
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
    }
    
    # 直接使用aiohttp进行文件下载，避免事件循环冲突
    import aiohttp
    
    # 获取文件大小
    async with aiohttp.ClientSession() as session:
        async with session.head(url, headers=headers) as response:
            file_size = int(response.headers.get('content-length', 0))
    
    # 创建一个tqdm进度条实例
    progress_bar = tqdm(total=file_size, unit='B', unit_scale=True, desc=f'Downloading {desc}')
    
    # 创建目录（如果不存在）
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    
    # 下载文件
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            with open(save_path, 'wb') as f:
                downloaded = 0
                async for chunk in response.content.iter_chunked(8192):  # 8KB chunks
                    if not chunk:
                        break
                    f.write(chunk)
                    chunk_size = len(chunk)
                    downloaded += chunk_size
                    progress_bar.update(chunk_size)
    
    progress_bar.close()

# 使用bilibili-api扫码登录
async def login_with_qrcode():
    import requests
    import queue
    
    # 定义请求头
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
    }
    
    # 用于线程间通信的队列
    result_queue = queue.Queue()
    
    # 获取二维码
    qr_data = requests.get(
        "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
        headers=HEADERS
    ).json()
    
    if qr_data["code"] != 0:
        raise Exception(f"获取二维码失败: {qr_data}")
    
    qr_url = qr_data["data"]["url"]
    qr_key = qr_data["data"]["qrcode_key"]
    
    # 生成二维码
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=5,
    )
    qr.add_data(qr_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    qrcode_file = f'./download/temp/qrcode.png'
    img.save(qrcode_file)
    
    # 创建一个线程来检查登录状态
    def check_login_status():
        try:
            while True:
                check_resp = requests.get(
                    "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                    params={"qrcode_key": qr_key},
                    headers=HEADERS
                )
                
                data = check_resp.json()
                
                if data["data"]["code"] == 0:  # 已扫码并确认
                    print("登录成功！")
                    # 将登录结果放入队列，而不是直接关闭窗口
                    cookies = requests.utils.dict_from_cookiejar(check_resp.cookies)
                    result_queue.put(("success", cookies))
                    return
                elif data["data"]["code"] == 86038:  # 二维码已失效
                    print("二维码已失效，请重新运行程序")
                    result_queue.put(("expired", None))
                    return
                elif data["data"]["code"] == 86090:  # 已扫码但未确认
                    print("已扫码，等待确认...")
                
                time.sleep(1)
        except Exception as e:
            print(f"登录检查异常: {e}")
            result_queue.put(("error", str(e)))
    
    # 启动检查线程
    login_thread = threading.Thread(target=check_login_status)
    login_thread.daemon = True
    login_thread.start()
    
    # 创建Tkinter窗口显示二维码（在主线程中）
    root = Tk()
    root.title("请使用B站APP扫描二维码登录")
    photo = PhotoImage(file=qrcode_file)
    canvas = Canvas(root, width=photo.width(), height=photo.height())
    canvas.pack()
    canvas.create_image(0, 0, image=photo, anchor="nw")
    
    # 定期检查登录状态队列
    def check_queue():
        try:
            status, data = result_queue.get_nowait()
            if status == "success":
                root.quit()  # 安全地通知事件循环退出
            elif status == "expired" or status == "error":
                root.quit()
        except queue.Empty:
            root.after(100, check_queue)  # 100ms后再次检查
    
    # 开始检查队列
    root.after(100, check_queue)
    
    # 启动Tkinter事件循环
    root.mainloop()
    
    # 安全地销毁窗口 (在mainloop退出后)
    root.destroy()
    
    # 删除临时文件
    os.remove(qrcode_file)
    
    # 从队列获取结果
    try:
        status, data = result_queue.get_nowait()
        if status == "success":
            login_cookies = data
        elif status == "expired":
            raise Exception("二维码已失效，请重新运行程序")
        else:
            raise Exception(f"登录出错: {data}")
    except queue.Empty:
        # 如果队列为空，可能是用户手动关闭了窗口，尝试最后一次检查
        try:
            check_resp = requests.get(
                "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                params={"qrcode_key": qr_key},
                headers=HEADERS
            )
            
            if check_resp.json()["data"]["code"] == 0:
                login_cookies = requests.utils.dict_from_cookiejar(check_resp.cookies)
            else:
                raise Exception("登录失败或已取消")
        except Exception as e:
            raise Exception(f"登录失败: {e}")
    
    # 创建凭证
    credential = Credential(
        sessdata=login_cookies.get("SESSDATA", ""),
        bili_jct=login_cookies.get("bili_jct", ""),
        buvid3=login_cookies.get("buvid3", "")
    )
    
    return credential

# 主程序
async def main():
    credential = None
    
    if os.path.exists('./bilibili.session'):
        try:
            with open('bilibili.session', 'r', encoding='utf-8') as file:
                cookies_data = json.loads(file.read())
                credential = Credential(
                    sessdata=cookies_data.get('SESSDATA', ''),
                    bili_jct=cookies_data.get('bili_jct', ''),
                    buvid3=cookies_data.get('buvid3', '')
                )
            
            # 验证凭证是否有效
            if not await credential.check_valid():
                print("凭证已过期，需要重新登录")
                credential = await login_with_qrcode()
        except Exception as e:
            print(f"读取会话文件出错: {e}")
            credential = await login_with_qrcode()
    else:
        credential = await login_with_qrcode()
    
    # 保存凭证到文件
    with open('bilibili.session', 'w', encoding='utf-8') as file:
        file.write(json.dumps(credential.get_cookies(), indent=4, ensure_ascii=False))
    
    print('请输入要下载的课程序号,只需要最后的ID')
    print('例如你的课程地址是https://www.bilibili.com/cheese/play/ss360')
    print('那么你的课程ID是 ss360 ')
    input_id = input('请输入要下载的课程序号: ')
    
    # 课程ID处理
    if input_id.startswith('ss'):
        season_id = int(input_id[2:])
        cheese_list = cheese.CheeseList(season_id=season_id, credential=credential)
    else:
        try:
            season_id = int(input_id)
            cheese_list = cheese.CheeseList(season_id=season_id, credential=credential)
        except ValueError:
            print("无效的课程ID，请确保输入正确的格式")
            return
    
    # 获取课程列表
    episodes = await cheese_list.get_list()
    
    index = 0
    for ep in tqdm(episodes):
        # 基础参数配置
        index += 1
        ep_id = ep.get_epid()
        title = (await ep.get_meta())['title'].replace(':', '_')
        
        # 获取音频和视频的链接，并设置本地保存的文件名
        filename_prefix = uuid.uuid4()
        download_url_data = await ep.get_download_url()
        
        # 解析下载链接
        detector = video.VideoDownloadURLDataDetecter(data=download_url_data)
        streams = detector.detect_best_streams()
        
        audio_file = f"./download/temp/{filename_prefix}_audio.m4s"
        video_file = f"./download/temp/{filename_prefix}_video.m4s"
        output_file = f"./download/{index}.{title}.mp4"
        
        # 下载音频和视频
        await download_file(streams[1].url, audio_file, f'{title} audio [1/3][{index}/{len(episodes)}]')
        await download_file(streams[0].url, video_file, f'{title} video [2/3][{index}/{len(episodes)}]')
        
        # 使用ffmpeg合并音频和视频
        cmd_line = f'ffmpeg -i "{video_file}" -i "{audio_file}" -c:v copy -map 0:v:0 -map 1:a:0 -shortest -y "{output_file}"'
        
        # 获取视频时长用于进度条
        video_meta = await ep.get_meta()
        duration = video_meta.get('duration', 0)
        
        encode_progress_bar = tqdm(total=duration, unit='second', desc=f'Encoding    {title} video [3/3][{index}/{len(episodes)}]')
        process = subprocess.Popen(cmd_line, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, encoding='utf-8')
        
        # 进度条展示编码进度
        for line in process.stdout:
            if line.startswith('size='):
                time_length = parse_time_2_sec(line)
                encode_progress_bar.update(time_length - encode_progress_bar.n)
        
        encode_progress_bar.close()
        
        # 删除临时文件
        os.remove(audio_file)
        os.remove(video_file)

if __name__ == "__main__":
    # 运行主程序
    asyncio.run(main())
