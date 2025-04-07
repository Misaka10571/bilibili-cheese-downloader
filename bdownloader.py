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
from asyncio import Semaphore
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures

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

# 格式化标题，使进度条显示整齐
def format_title(title, max_length=20):
    if len(title) > max_length:
        return title[:max_length-3] + "..."
    else:
        # 填充空格使所有标题长度一致
        return title.ljust(max_length)

# 下载文件
async def download_file(url, save_path, desc, task_index, total_tasks, task_type):
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
    
    # 创建一个tqdm进度条实例，使用统一的格式
    progress_bar = tqdm(
        total=file_size, 
        unit='B', 
        unit_scale=True, 
        desc=f'[{task_index}/{total_tasks}] {desc} {task_type}'
    )
    
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

# 清理文件名，移除非法字符
def sanitize_filename(filename):
    # 替换Windows不允许的文件名字符
    illegal_chars = r'[<>:"/\\|?*]'
    sanitized = re.sub(illegal_chars, '_', filename)
    # 去除前后空格
    sanitized = sanitized.strip()
    # 确保文件名不为空，如果为空则用默认名称
    if not sanitized:
        sanitized = "未命名视频"
    return sanitized

# 在FFmpeg中合成视频，在单独的线程中运行
def ffmpeg_merge(video_file, audio_file, output_file, title, index, total_count, duration):
    try:
        # 创建统一格式的编码进度条
        encode_progress_bar = tqdm(
            total=duration, 
            unit='second', 
            desc=f'[{index}/{total_count}] {title} video [3/3]'
        )
        
        cmd_line = f'ffmpeg -i "{video_file}" -i "{audio_file}" -c:v copy -map 0:v:0 -map 1:a:0 -shortest -y "{output_file}"'
        process = subprocess.Popen(cmd_line, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, 
                                  universal_newlines=True, encoding='utf-8')
        
        # 进度条展示编码进度
        for line in process.stdout:
            if line.startswith('size='):
                time_length = parse_time_2_sec(line)
                encode_progress_bar.update(time_length - encode_progress_bar.n)
        
        # 等待进程完成并检查返回码
        return_code = process.wait()
        encode_progress_bar.close()
        
        # 检查ffmpeg是否成功完成
        if return_code != 0:
            raise Exception(f"FFmpeg失败，返回码: {return_code}")
            
        # 检查输出文件是否存在且大小大于0
        if not os.path.exists(output_file):
            raise Exception(f"输出文件不存在: {output_file}")
        if os.path.getsize(output_file) == 0:
            raise Exception(f"输出文件大小为0: {output_file}")
            
        print(f"视频 [{index}/{total_count}] '{title}' 合成成功: {output_file}")
        
        # 删除临时文件
        try:
            if os.path.exists(audio_file):
                os.remove(audio_file)
            if os.path.exists(video_file):
                os.remove(video_file)
        except Exception as e:
            print(f"清理临时文件时出错，但不影响结果: {e}")
        
        return True
    except Exception as e:
        print(f"合成视频 {index} 时出错: {e}")
        return False

# 处理单个视频的下载和合成
async def process_episode(ep, index, total_count, semaphore, course_folder, ffmpeg_executor):
    async with semaphore:  # 使用信号量控制并发数
        try:
            # 基础参数配置
            ep_id = ep.get_epid()
            original_title = (await ep.get_meta())['title']
            # 替换非法字符
            safe_title = sanitize_filename(original_title)
            title = format_title(safe_title)  # 格式化标题用于显示
            
            # 获取音频和视频的链接，并设置本地保存的文件名
            filename_prefix = uuid.uuid4()
            download_url_data = await ep.get_download_url()
            
            # 解析下载链接
            detector = video.VideoDownloadURLDataDetecter(data=download_url_data)
            streams = detector.detect_best_streams()
            
            audio_file = f"./download/temp/{filename_prefix}_audio.m4s"
            video_file = f"./download/temp/{filename_prefix}_video.m4s"
            # 使用课程文件夹保存文件
            output_file = f"./download/{course_folder}/{index}.{safe_title}.mp4"
            
            # 确保课程文件夹存在
            os.makedirs(f"./download/{course_folder}", exist_ok=True)
            
            # 下载音频和视频
            await download_file(streams[1].url, audio_file, title, index, total_count, "audio [1/3]")
            await download_file(streams[0].url, video_file, title, index, total_count, "video [2/3]")
            
            # 验证下载的文件是否存在且大小大于0
            if not os.path.exists(audio_file) or os.path.getsize(audio_file) == 0:
                raise Exception(f"音频文件下载失败或大小为0: {audio_file}")
            if not os.path.exists(video_file) or os.path.getsize(video_file) == 0:
                raise Exception(f"视频文件下载失败或大小为0: {video_file}")
            
            # 获取视频时长用于进度条
            video_meta = await ep.get_meta()
            duration = video_meta.get('duration', 0)
            
            # 将ffmpeg合成提交到线程池，然后继续下一个下载任务
            ffmpeg_executor.submit(
                ffmpeg_merge, 
                video_file, 
                audio_file, 
                output_file, 
                title, 
                index, 
                total_count, 
                duration
            )
            
            # 不等待ffmpeg完成，直接返回成功，以便继续下载下一个视频
            return True
            
        except Exception as e:
            print(f"处理视频 {index} 时出错: {e}")
            # 尝试清理可能的临时文件
            try:
                if 'audio_file' in locals() and os.path.exists(audio_file):
                    os.remove(audio_file)
                if 'video_file' in locals() and os.path.exists(video_file):
                    os.remove(video_file)
            except Exception:
                pass
            return False

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
    try:
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
        
        # 获取课程信息 - 修正方法为get_meta()
        try:
            course_info = await cheese_list.get_meta()
            if 'title' not in course_info:
                print(f"获取课程信息失败，API返回: {course_info}")
                return
                
            course_title = sanitize_filename(course_info['title'])
            print(f"正在下载课程: {course_title}")
            
            # 确保课程文件夹存在
            course_folder = course_title
            if not os.path.exists(f"./download/{course_folder}"):
                os.makedirs(f"./download/{course_folder}")
            
            # 获取课程列表
            episodes = await cheese_list.get_list()
            
            # 询问用户并行下载数量
            concurrent_downloads = 1  # 默认值
            try:
                user_input = input('请输入并行下载的数量（默认为1）: ').strip()
                if (user_input):
                    concurrent_downloads = int(user_input)
                    if concurrent_downloads < 1:
                        concurrent_downloads = 1
                    elif concurrent_downloads > len(episodes):
                        concurrent_downloads = len(episodes)
            except ValueError:
                print("输入无效，使用默认值1")
                concurrent_downloads = 1
            
            # 询问用户并行合成数量
            concurrent_ffmpeg = 1  # 默认值
            try:
                user_input = input('请输入并行合成的数量（默认为1）: ').strip()
                if user_input:
                    concurrent_ffmpeg = int(user_input)
                    if concurrent_ffmpeg < 1:
                        concurrent_ffmpeg = 1
                    elif concurrent_ffmpeg > len(episodes):
                        concurrent_ffmpeg = len(episodes)
            except ValueError:
                print("输入无效，使用默认值1")
                concurrent_ffmpeg = 1
            
            print(f"将使用 {concurrent_downloads} 个并行任务下载, {concurrent_ffmpeg} 个并行任务合成")
            
            # 创建信号量以限制并发下载数
            semaphore = Semaphore(concurrent_downloads)
            
            # 创建用于ffmpeg合成的线程池
            with ThreadPoolExecutor(max_workers=concurrent_ffmpeg) as ffmpeg_executor:
                # 创建所有下载任务
                tasks = []
                for i, ep in enumerate(episodes, 1):
                    task = asyncio.create_task(process_episode(
                        ep, i, len(episodes), semaphore, course_folder, ffmpeg_executor
                    ))
                    tasks.append(task)
                
                # 等待所有下载任务完成
                results = await asyncio.gather(*tasks)
                
                # 等待所有ffmpeg合成任务完成
                print("所有下载任务已完成，等待剩余的合成任务完成...")
                ffmpeg_executor.shutdown(wait=True)
            
            # 统计下载结果
            success_count = results.count(True)
            failed_count = len(results) - success_count
            print(f"下载完成，成功: {success_count}, 失败: {failed_count}")
            
        except Exception as e:
            print(f"处理课程信息时出错: {e}")
            import traceback
            traceback.print_exc()
            
    except Exception as e:
        print(f"处理课程ID时出错: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    # 运行主程序
    asyncio.run(main())
