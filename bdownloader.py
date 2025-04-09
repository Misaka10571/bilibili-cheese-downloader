from os import path, makedirs, remove
from subprocess import run, Popen, PIPE, STDOUT
from time import sleep
from json import loads, dumps
from re import compile
from uuid import uuid4
from tqdm import tqdm
from threading import RLock, Lock
from asyncio import create_task, gather, Semaphore, run as asyncio_run, CancelledError, sleep as asyncio_sleep
from concurrent.futures import ThreadPoolExecutor
from logging import basicConfig, FileHandler, StreamHandler, getLogger, INFO, ERROR, WARNING
from configparser import ConfigParser
from pathlib import Path

# 导入bilibili-api库
from bilibili_api import Credential, cheese, video, sync
from bilibili_api import login_v2

# 设置日志系统
basicConfig(
    level=INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        FileHandler("bdownloader.log", encoding='utf-8'),
        StreamHandler()
    ]
)
logger = getLogger("BDownloader")

# 全局变量，存储NVIDIA GPU支持状态
NVIDIA_GPU_SUPPORTED = None
FORCE_GPU_MODE = None  # 强制使用GPU模式

# 检测FFmpeg是否正确安装
def check_ffmpeg():
    try:
        result = run(['ffmpeg', '-version'], stdout=PIPE, stderr=PIPE, text=True)
        if result.returncode == 0:
            logger.info("FFmpeg 已正确安装")
            return True
        else:
            logger.error("FFmpeg 安装异常，无法正常运行")
            return False
    except FileNotFoundError:
        logger.error("未找到 FFmpeg")
        print("\n错误: FFmpeg 未安装或未添加到系统 PATH 中或放置在当前文件夹下。")
        print("请安装 FFmpeg 后再运行此程序。")
        print("安装指南: https://ffmpeg.org/download.html")
        return False
    except Exception as e:
        logger.error(f"检测 FFmpeg 时出错: {e}")
        print(f"\n错误: 检测 FFmpeg 时出现问题: {e}")
        print("请确保 FFmpeg 已正确安装")
        return False

# 创建下载目录
if not path.exists('./download/temp'):
    makedirs('./download/temp')

# 全局变量，用于管理进度条
PROGRESS_BARS = {}
PROGRESS_BAR_LOCK = RLock()  # 使用可重入锁

# 预编译正则表达式，提高性能
TIME_PATTERN = compile(r'(\d{2}):(\d{2}):(\d{2})')
ILLEGAL_FILENAME_CHARS = compile(r'[<>:"/\\|?*]')

# 加载配置文件
def load_config():
    config = ConfigParser()
    config_file = Path('./config.ini')
    
    # 默认配置
    default_config = {
        'General': {
            'concurrent_downloads': '2',
            'concurrent_ffmpeg': '1',
            'gpu_mode': 'auto'  # 'auto', 'force_gpu', 'force_cpu'
        }
    }
    
    # 如果配置文件不存在，创建一个默认配置
    if not config_file.exists():
        for section, options in default_config.items():
            if not config.has_section(section):
                config.add_section(section)
            for key, value in options.items():
                config.set(section, key, value)
        
        with open(config_file, 'w', encoding='utf-8') as f:
            config.write(f)
        logger.info(f"已创建默认配置文件: {config_file}")
    
    # 读取配置文件
    config.read(config_file, encoding='utf-8')
    return config

# 检测系统是否支持NVIDIA GPU加速（包括NVENC和NVDEC）
def check_nvidia_gpu_support(force_mode=None):
    global NVIDIA_GPU_SUPPORTED, FORCE_GPU_MODE
    
    # 如果指定了强制模式，使用它
    if force_mode is not None:
        FORCE_GPU_MODE = force_mode
        NVIDIA_GPU_SUPPORTED = force_mode
        mode_str = "GPU" if force_mode else "CPU"
        logger.warning(f"已手动设置为强制使用{mode_str}模式")
        return FORCE_GPU_MODE
    
    # 如果已经检测过，且没有强制模式，直接返回结果
    if NVIDIA_GPU_SUPPORTED is not None and FORCE_GPU_MODE is None:
        return NVIDIA_GPU_SUPPORTED
    
    # 如果已设置了强制模式，使用它
    if FORCE_GPU_MODE is not None:
        return FORCE_GPU_MODE
    
    try:
        logger.info("正在检测系统是否支持NVIDIA GPU加速...")
        
        # 检查GPU是否可用 - 方法1：检查nvenc编码器
        encoders_output = ""
        try:
            encoders_result = run(['ffmpeg', '-encoders'], stdout=PIPE, stderr=PIPE, text=True)
            encoders_output = encoders_result.stdout.lower()
            nvenc_available = 'h264_nvenc' in encoders_output
            logger.info(f"- NVENC编码器检测: {'✅ 可用' if nvenc_available else '❌ 不可用'}")
        except Exception as e:
            nvenc_available = False
            logger.error(f"- NVENC编码器检测失败: {e}")
        
        # 检查GPU是否可用 - 方法2：检查CUDA硬件加速
        hwaccels_output = ""
        try:
            hwaccels_result = run(['ffmpeg', '-hwaccels'], stdout=PIPE, stderr=PIPE, text=True)
            hwaccels_output = hwaccels_result.stdout.lower()
            cuda_available = 'cuda' in hwaccels_output
            logger.info(f"- CUDA硬件加速检测: {'✅ 可用' if cuda_available else '❌ 不可用'}")
        except Exception as e:
            cuda_available = False
            logger.error(f"- CUDA硬件加速检测失败: {e}")
            
        # 检查GPU是否可用 - 方法3：使用nvidia-smi检查GPU
        nvidia_smi_available = False
        try:
            nvidia_smi_result = run(['nvidia-smi'], stdout=PIPE, stderr=PIPE, text=True)
            nvidia_smi_available = nvidia_smi_result.returncode == 0
            logger.info(f"- NVIDIA GPU检测: {'✅ 可用' if nvidia_smi_available else '❌ 不可用'}")
        except Exception:
            nvidia_smi_available = False
            logger.info("- NVIDIA GPU检测: ❌ 不可用 (无法执行nvidia-smi)")
        
        # 综合判断：如果任一方法检测到GPU，就认为支持GPU
        NVIDIA_GPU_SUPPORTED = nvenc_available or (cuda_available and nvidia_smi_available)
        
        if NVIDIA_GPU_SUPPORTED:
            logger.info("✅ 检测到NVIDIA GPU加速支持，将在视频合成时使用")
        else:
            logger.info("❌ 未检测到可用的NVIDIA GPU加速，将使用CPU模式")
            if nvenc_available and not nvidia_smi_available:
                logger.info("   提示: 检测到NVENC但未找到NVIDIA GPU，可能是驱动问题")
            if nvidia_smi_available and not nvenc_available:
                logger.info("   提示: 检测到NVIDIA GPU但未找到NVENC，可能需要安装NVIDIA驱动或更新FFmpeg")
        
        return NVIDIA_GPU_SUPPORTED
    except Exception as e:
        logger.error(f"检测NVIDIA GPU支持时出错: {e}，将使用CPU模式")
        NVIDIA_GPU_SUPPORTED = False
        return False

# 解析时间为秒
def parse_time_2_sec(s):
    duration_match = TIME_PATTERN.search(s)
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

# 进度条管理类
class ProgressManager:
    def __init__(self):
        self.bars = {}
        self.lock = RLock()
    
    def create_bar(self, key, total, desc, position=0, unit='B', leave=False):
        with self.lock:
            bar = tqdm(
                total=total, 
                unit=unit, 
                unit_scale=True,
                desc=desc,
                dynamic_ncols=True,
                position=position,
                leave=leave,
                smoothing=0.01,
                mininterval=0.2,
                maxinterval=1.0,
                miniters=None,
                ncols=100
            )
            self.bars[key] = bar
            return bar
    
    def update_bar(self, key, value):
        with self.lock:
            if key in self.bars and not self.bars[key].disable:
                self.bars[key].update(value)
    
    def close_bar(self, key):
        with self.lock:
            if key in self.bars:
                self.bars[key].close()
                del self.bars[key]
    
    def close_all(self):
        with self.lock:
            for key, bar in list(self.bars.items()):
                try:
                    bar.close()
                except:
                    pass
            self.bars.clear()

# 初始化进度条管理器
progress_mgr = ProgressManager()

# 下载文件 - 改进异常处理和断点续传
async def download_file(url, save_path, desc, task_index, total_tasks, task_type):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
    }
    
    from aiohttp import ClientSession
    progress_bar_key = f"download_{task_index}_{task_type}"
    
    try:
        # 检查是否已存在部分下载的文件
        downloaded = 0
        if path.exists(save_path):
            downloaded = path.getsize(save_path)
            logger.info(f"发现已下载文件: {save_path}，大小: {downloaded} 字节")
        
        # 获取文件大小
        async with ClientSession() as session:
            async with session.head(url, headers=headers) as response:
                file_size = int(response.headers.get('content-length', 0))
        
            # 如果文件已下载完成，则跳过
            if downloaded == file_size and file_size > 0:
                logger.info(f"文件已完整下载，跳过: {save_path}")
                return True
        
            # 创建目录（如果不存在）
            makedirs(path.dirname(save_path), exist_ok=True)
        
            # 创建进度条
            position = task_index % 10
            progress_bar = progress_mgr.create_bar(
                progress_bar_key,
                file_size, 
                f'[{task_index}/{total_tasks}] {desc} {task_type}',
                position
            )
        
            # 如果文件已部分下载，则设置进度条初始值
            if downloaded > 0:
                progress_bar.update(downloaded)
                headers['Range'] = f'bytes={downloaded}-'
                logger.info(f"从断点 {downloaded}/{file_size} 字节继续下载...")
        
            # 下载文件
            mode = 'ab' if downloaded > 0 else 'wb'  # 如果已部分下载，则使用追加模式
            try:
                async with session.get(url, headers=headers) as response:
                    with open(save_path, mode) as f:
                        chunk_size = 32768
                        async for chunk in response.content.iter_chunked(chunk_size):
                            if not chunk:
                                break
                            f.write(chunk)
                            progress_mgr.update_bar(progress_bar_key, len(chunk))
            except CancelledError:
                logger.warning(f"下载任务被取消: {save_path}")
                raise
            except Exception as e:
                logger.error(f"下载过程中出错: {e}，将尝试恢复")
                # 恢复下载的逻辑已经在外层函数中处理
                raise
        
        # 完成并清理资源
        progress_mgr.close_bar(progress_bar_key)
        
        # 检查文件完整性
        actual_size = path.getsize(save_path)
        if actual_size != file_size and file_size > 0:
            logger.warning(f"文件大小不匹配，预期: {file_size}，实际: {actual_size}")
            if actual_size < file_size * 0.98:
                raise Exception(f"下载不完整，需要重新下载")
                
        logger.info(f"✓ 完成下载: [{task_index}/{total_tasks}] {desc} {task_type}")
        return True
        
    except Exception as e:
        logger.error(f"下载失败: [{task_index}] {desc} - {e}")
        progress_mgr.close_bar(progress_bar_key)
        raise

# 使用bilibili-api扫码登录
async def login_with_qrcode():
    # 创建二维码登录实例
    qr = login_v2.QrCodeLogin(platform=login_v2.QrCodeLoginChannel.WEB)
    await qr.generate_qrcode()
    print(qr.get_qrcode_terminal())
    print("请使用B站APP扫描以上二维码: （如果二维码面积过大无法显示，可以使用 Ctrl + 鼠标滚轮缩放）")


    while not qr.has_done():
        state = await qr.check_state()
        print(f"登录状态: {state}")
        await asyncio_sleep(1)

    print("登录成功")
    return qr.get_credential()

# 清理文件名，移除非法字符
def sanitize_filename(filename):
    # 替换Windows不允许的文件名字符
    sanitized = ILLEGAL_FILENAME_CHARS.sub('_', filename)
    # 去除前后空格
    sanitized = sanitized.strip()
    # 确保文件名不为空，如果为空则用默认名称
    if not sanitized:
        sanitized = "未命名视频"
    return sanitized

# 构建FFmpeg命令行
def build_ffmpeg_cmd(video_file, audio_file, output_file, use_gpu=False, width=1920, height=1080, attempt=0):
    # GPU模式命令选项
    gpu_cmd_options = [
        # 最基本的NVENC配置，兼容性最佳
        f'ffmpeg -y -i "{video_file}" -i "{audio_file}" -c:v h264_nvenc -preset slow -profile:v main -level auto -c:a copy -map 0:v:0 -map 1:a:0 -shortest "{output_file}"',
        
        # 不使用任何预设，让驱动器决定
        f'ffmpeg -y -i "{video_file}" -i "{audio_file}" -c:v h264_nvenc -c:a copy -map 0:v:0 -map 1:a:0 -shortest "{output_file}"',
        
        # 尝试CUDA硬件加速，但使用较低的比特率
        f'ffmpeg -y -hwaccel cuda -i "{video_file}" -i "{audio_file}" -c:v h264_nvenc -b:v 2M -maxrate 4M -bufsize 8M -c:a copy -map 0:v:0 -map 1:a:0 -shortest "{output_file}"',
        
        # 最简单的GPU加速配置 + 视频缩放确保尺寸正确
        f'ffmpeg -y -i "{video_file}" -i "{audio_file}" -vf "scale={width}:{height},format=yuv420p" -c:v h264_nvenc -c:a copy -map 0:v:0 -map 1:a:0 -shortest "{output_file}"'
    ]
    
    # CPU模式命令选项
    cpu_cmd_options = [
        # 最基本的CPU复制模式
        f'ffmpeg -y -i "{video_file}" -i "{audio_file}" -c:v copy -c:a copy -map 0:v:0 -map 1:a:0 -shortest "{output_file}"',
        
        # CPU编码模式 - 使用libx264（更兼容但更慢）
        f'ffmpeg -y -i "{video_file}" -i "{audio_file}" -c:v libx264 -preset ultrafast -crf 23 -c:a copy -map 0:v:0 -map 1:a:0 -shortest "{output_file}"'
    ]
    
    # 首先尝试流复制模式（最快）
    if attempt == 0:
        return f'ffmpeg -y -i "{video_file}" -i "{audio_file}" -c:v copy -c:a copy -map 0:v:0 -map 1:a:0 -shortest "{output_file}"'
    
    # 如果流复制失败，根据GPU支持情况选择命令
    if use_gpu:
        if attempt <= len(gpu_cmd_options):
            return gpu_cmd_options[attempt - 1]
    
    # 如果GPU命令都失败或不支持GPU，使用CPU命令
    cpu_index = attempt - 1
    if use_gpu:
        cpu_index = attempt - len(gpu_cmd_options) - 1
    
    if cpu_index < len(cpu_cmd_options):
        return cpu_cmd_options[cpu_index]
    
    # 如果所有尝试都失败，返回最基本的CPU命令
    return cpu_cmd_options[0]

# 在FFmpeg中合成视频，改进错误处理和命令构建
def ffmpeg_merge(video_file, audio_file, output_file, title, index, total_count, duration):
    progress_bar_key = f"ffmpeg_{index}"
    
    try:
        # 创建进度条
        position = index % 10
        encode_progress_bar = progress_mgr.create_bar(
            progress_bar_key,
            duration, 
            f'[{index}/{total_count}] {title} video [3/3]',
            position,
            unit='second'
        )
        
        success = False
        attempt = 0
        max_attempts = 6  # 最多尝试次数
        
        # 获取视频信息，以便确定正确的参数
        width, height = 1920, 1080
        try:
            video_info_cmd = f'ffprobe -v error -select_streams v:0 -show_entries stream=width,height,codec_name -of json "{video_file}"'
            video_info = run(video_info_cmd, shell=True, stdout=PIPE, stderr=PIPE, text=True)
            video_info_json = loads(video_info.stdout)
            width = video_info_json['streams'][0]['width']
            height = video_info_json['streams'][0]['height']
            logger.info(f"检测到视频分辨率: {width}x{height}")
        except Exception as e:
            logger.error(f"无法获取视频信息: {e}, 将使用默认参数")
        
        # 尝试不同的合成方式，直到成功或达到最大尝试次数
        while not success and attempt < max_attempts:
            try:
                # 构建命令行
                cmd_line = build_ffmpeg_cmd(
                    video_file, 
                    audio_file, 
                    output_file, 
                    use_gpu=NVIDIA_GPU_SUPPORTED, 
                    width=width, 
                    height=height,
                    attempt=attempt
                )
                
                # 记录当前尝试
                mode = "流复制" if attempt == 0 else "GPU" if NVIDIA_GPU_SUPPORTED and attempt < 5 else "CPU"
                logger.info(f"尝试{mode}方案 {attempt+1}/{max_attempts} 合成视频 [{index}/{total_count}]")
                
                # 重置进度条
                if attempt > 0:
                    encode_progress_bar.reset()
                
                # 执行命令
                process = Popen(
                    cmd_line, 
                    stdout=PIPE, 
                    stderr=STDOUT, 
                    universal_newlines=True, 
                    encoding='utf-8'
                )
                
                # 处理输出并更新进度条
                for line in process.stdout:
                    if line.startswith('size='):
                        time_length = parse_time_2_sec(line)
                        progress_mgr.update_bar(progress_bar_key, time_length - encode_progress_bar.n)
                
                # 检查命令执行结果
                return_code = process.wait()
                if return_code == 0:
                    success = True
                    logger.info(f"{mode}方案 {attempt+1} 成功!")
                else:
                    logger.warning(f"{mode}方案 {attempt+1} 失败，返回码: {return_code}")
                    attempt += 1
            except Exception as e:
                logger.error(f"{mode}方案 {attempt+1} 异常: {e}")
                attempt += 1
        
        # 关闭进度条
        progress_mgr.close_bar(progress_bar_key)
        
        # 检查最终结果
        if not success:
            raise Exception("所有编码方式都失败了，无法合成视频")
        
        # 检查输出文件
        if not path.exists(output_file):
            raise Exception(f"输出文件不存在: {output_file}")
        if path.getsize(output_file) == 0:
            raise Exception(f"输出文件大小为0: {output_file}")
            
        logger.info(f"视频 [{index}/{total_count}] '{title}' 合成成功: {output_file}")
        
        # 删除临时文件
        try:
            if path.exists(audio_file):
                remove(audio_file)
            if path.exists(video_file):
                remove(video_file)
        except Exception as e:
            logger.warning(f"清理临时文件时出错，但不影响结果: {e}")
        
        return True
    except Exception as e:
        logger.error(f"合成视频 {index} 时出错: {e}")
        progress_mgr.close_bar(progress_bar_key)
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
            filename_prefix = uuid4()
            download_url_data = await ep.get_download_url()
            
            # 解析下载链接
            detector = video.VideoDownloadURLDataDetecter(data=download_url_data)
            streams = detector.detect_best_streams()
            
            audio_file = f"./download/temp/{filename_prefix}_audio.m4s"
            video_file = f"./download/temp/{filename_prefix}_video.m4s"
            # 使用课程文件夹保存文件
            output_file = f"./download/{course_folder}/{index}.{safe_title}.mp4"
            
            # 确保课程文件夹存在
            makedirs(f"./download/{course_folder}", exist_ok=True)
            
            # 下载音频和视频
            await download_file(streams[1].url, audio_file, title, index, total_count, "audio [1/3]")
            await download_file(streams[0].url, video_file, title, index, total_count, "video [2/3]")
            
            # 验证下载的文件是否存在且大小大于0
            if not path.exists(audio_file) or path.getsize(audio_file) == 0:
                raise Exception(f"音频文件下载失败或大小为0: {audio_file}")
            if not path.exists(video_file) or path.getsize(video_file) == 0:
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
            return {"success": True, "index": index, "episode": ep}
            
        except Exception as e:
            logger.error(f"处理视频 {index} 时出错: {e}")
            # 尝试清理可能的临时文件
            try:
                if 'audio_file' in locals() and path.exists(audio_file):
                    remove(audio_file)
                if 'video_file' in locals() and path.exists(video_file):
                    remove(video_file)
            except Exception:
                pass
            # 返回失败信息，包含视频信息以便重试
            return {"success": False, "index": index, "episode": ep, "error": str(e)}

# 清理所有进度条的函数
def cleanup_progress_bars():
    with PROGRESS_BAR_LOCK:
        for key, bar in list(PROGRESS_BARS.items()):
            try:
                if bar:
                    bar.close()
            except:
                pass
        PROGRESS_BARS.clear()

# 主程序 - 添加配置文件支持和改进错误处理
async def main():
    try:
        # 检查FFmpeg是否已安装
        if not check_ffmpeg():
            print("\n程序无法继续，请安装FFmpeg后重试。")
            return
            
        # 加载配置
        config = load_config()
        default_concurrent_downloads = config.getint('General', 'concurrent_downloads', fallback=2)
        default_concurrent_ffmpeg = config.getint('General', 'concurrent_ffmpeg', fallback=1)
        gpu_mode = config.get('General', 'gpu_mode', fallback='auto')
        
        # 询问用户是否要强制使用GPU/CPU模式
        print("\n== 硬件加速设置 ==")
        print("1. 自动检测 (默认)")
        print("2. 强制使用GPU")
        print("3. 强制使用CPU")
        print(f"当前配置: {gpu_mode}")
        
        choice = input("请选择 (输入数字1-3，直接回车使用配置文件设置): ").strip()
        
        force_mode = None
        if choice == '1' or (not choice and gpu_mode == 'auto'):
            force_mode = None
        elif choice == '2' or (not choice and gpu_mode == 'force_gpu'):
            force_mode = True
        elif choice == '3' or (not choice and gpu_mode == 'force_cpu'):
            force_mode = False
        
        # 检测NVIDIA GPU支持状态
        check_nvidia_gpu_support(force_mode)
        
        credential = None
        
        if path.exists('./bilibili.session'):
            try:
                with open('bilibili.session', 'r', encoding='utf-8') as file:
                    cookies_data = loads(file.read())
                    credential = Credential(
                        sessdata=cookies_data.get('SESSDATA', ''),
                        bili_jct=cookies_data.get('bili_jct', ''),
                        buvid3=cookies_data.get('buvid3', '')
                    )
                
                # 验证凭证是否有效
                if not await credential.check_valid():
                    logger.info("凭证已过期，需要重新登录")
                    credential = await login_with_qrcode()
            except Exception as e:
                logger.error(f"读取会话文件出错: {e}")
                credential = await login_with_qrcode()
        else:
            credential = await login_with_qrcode()
        
        # 保存凭证到文件
        with open('bilibili.session', 'w', encoding='utf-8') as file:
            file.write(dumps(credential.get_cookies(), indent=4, ensure_ascii=False))
        
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
                if not path.exists(f"./download/{course_folder}"):
                    makedirs(f"./download/{course_folder}")
                
                # 获取课程列表
                episodes = await cheese_list.get_list()
                
                # 询问用户并行下载数量，使用配置文件默认值
                concurrent_downloads = default_concurrent_downloads
                try:
                    user_input = input(f'请输入并行下载的数量（默认为{default_concurrent_downloads}）: ').strip()
                    if user_input:
                        concurrent_downloads = int(user_input)
                        if concurrent_downloads < 1:
                            concurrent_downloads = 1
                        elif concurrent_downloads > len(episodes):
                            concurrent_downloads = len(episodes)
                except ValueError:
                    logger.warning(f"输入无效，使用默认值{default_concurrent_downloads}")
                    concurrent_downloads = default_concurrent_downloads
                
                # 询问用户并行合成数量，使用配置文件默认值
                concurrent_ffmpeg = default_concurrent_ffmpeg
                try:
                    user_input = input(f'请输入并行合成的数量（默认为{default_concurrent_ffmpeg}）: ').strip()
                    if user_input:
                        concurrent_ffmpeg = int(user_input)
                        if concurrent_ffmpeg < 1:
                            concurrent_ffmpeg = 1
                        elif concurrent_ffmpeg > len(episodes):
                            concurrent_ffmpeg = len(episodes)
                except ValueError:
                    logger.warning(f"输入无效，使用默认值{default_concurrent_ffmpeg}")
                    concurrent_ffmpeg = default_concurrent_ffmpeg
                
                print(f"将使用 {concurrent_downloads} 个并行任务下载, {concurrent_ffmpeg} 个并行任务合成")
                
                # 创建信号量以限制并发下载数
                semaphore = Semaphore(concurrent_downloads)
                
                # 创建用于ffmpeg合成的线程池
                with ThreadPoolExecutor(max_workers=concurrent_ffmpeg) as ffmpeg_executor:
                    # 创建所有下载任务
                    tasks = []
                    for i, ep in enumerate(episodes, 1):
                        task = create_task(process_episode(
                            ep, i, len(episodes), semaphore, course_folder, ffmpeg_executor
                        ))
                        tasks.append(task)
                    
                    # 等待所有下载任务完成
                    results = await gather(*tasks)
                    
                    # 筛选出失败的任务
                    failed_tasks = [r for r in results if not r["success"]]
                    success_count = len(results) - len(failed_tasks)
                    
                    # 如果有失败任务但不是全部失败，尝试重新下载
                    if failed_tasks and len(failed_tasks) < len(results):
                        print(f"\n有 {len(failed_tasks)} 个视频下载失败，正在尝试重新下载...")
                        
                        # 创建重试任务
                        retry_tasks = []
                        for failed in failed_tasks:
                            print(f"重新下载视频 [{failed['index']}/{len(episodes)}]...")
                            task = create_task(process_episode(
                                failed["episode"], 
                                failed["index"], 
                                len(episodes), 
                                semaphore, 
                                course_folder, 
                                ffmpeg_executor
                            ))
                            retry_tasks.append(task)
                        
                        # 等待重试任务完成
                        if retry_tasks:
                            retry_results = await gather(*retry_tasks)
                            
                            # 更新成功计数
                            retry_success = sum(1 for r in retry_results if r["success"])
                            success_count += retry_success
                            
                            print(f"重试结果：成功: {retry_success}, 失败: {len(retry_tasks) - retry_success}")
                    
                    # 等待所有ffmpeg合成任务完成
                    print("所有下载任务已完成，等待剩余的合成任务完成...")
                    ffmpeg_executor.shutdown(wait=True)
                
                # 统计下载结果
                failed_count = len(episodes) - success_count
                print(f"下载完成，成功: {success_count}, 失败: {failed_count}")
                
            except Exception as e:
                logger.error(f"处理课程信息时出错: {e}")
                from traceback import print_exc
                print_exc()
                
        except Exception as e:
            logger.error(f"处理课程ID时出错: {e}")
            from traceback import print_exc
            print_exc()
        
        # 在主函数结束前确保清理所有进度条
        progress_mgr.close_all()
        
    except Exception as e:
        logger.error(f"程序执行出错: {e}")
        from traceback import print_exc
        print_exc()
        # 确保清理资源
        progress_mgr.close_all()

if __name__ == "__main__":
    # 运行主程序
    try:
        asyncio_run(main())
    except KeyboardInterrupt:
        logger.warning("\n程序被用户中断")
        progress_mgr.close_all()
    except Exception as e:
        logger.error(f"程序异常退出: {e}")
        progress_mgr.close_all()
