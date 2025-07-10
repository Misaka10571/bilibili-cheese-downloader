from os import path, makedirs, remove, listdir
from subprocess import run, Popen, PIPE, STDOUT, TimeoutExpired
from time import sleep
from json import loads, dumps
from re import compile
from uuid import uuid4
from tqdm import tqdm
from threading import RLock, Lock
from asyncio import create_task, gather, Semaphore, run as asyncio_run, CancelledError, sleep as asyncio_sleep
from concurrent.futures import ThreadPoolExecutor, as_completed
from logging import basicConfig, FileHandler, StreamHandler, getLogger, INFO, ERROR, WARNING
from configparser import ConfigParser
from pathlib import Path
import shutil

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

# 添加 detect_video_codec 函数实现
def detect_video_codec(video_path):
    try:
        cmd = [
            'ffprobe',
            '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_name',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            video_path
        ]
        result = run(cmd, stdout=PIPE, stderr=PIPE, text=True, timeout=10)
        if result.returncode == 0:
            return result.stdout.strip()
        else:
            logger.error(f"检测视频编码失败: {result.stderr}")
            return None
    except TimeoutExpired:
        logger.error("检测视频编码超时")
        return None
    except Exception as e:
        logger.error(f"检测视频编码时出错: {e}")
        return None

# 修改检测H265支持的部分
def check_h265_support(use_gpu):
    try:
        encoders_result = run(['ffmpeg', '-encoders'], stdout=PIPE, stderr=PIPE, text=True, timeout=15)
        encoders_output = encoders_result.stdout.lower()
        
        # 检查所有可能的H265编码器
        h265_encoders = [
            'hevc_nvenc', 'h265_nvenc', 
            'hevc_vaapi', 'h265_vaapi',
            'hevc_amf', 'h265_amf',
            'libx265', 'hevc', 'h265'
        ]
        
        # 检查系统支持哪些编码器
        supported_encoders = [enc for enc in h265_encoders if enc in encoders_output]
        
        if supported_encoders:
            logger.info(f"检测到支持的H265编码器: {', '.join(supported_encoders)}")
        else:
            logger.warning("未检测到任何H265编码器支持")
        
        # 如果有支持的编码器则返回True
        return len(supported_encoders) > 0
    except TimeoutExpired:
        logger.error("检测H265支持超时")
        return False
    except Exception as e:
        logger.error(f"检测H265支持时出错: {e}")
        return False

# 检测FFmpeg是否正确安装
def check_ffmpeg():
    try:
        result = run(['ffmpeg', '-version'], stdout=PIPE, stderr=PIPE, text=True, timeout=15)
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
    except TimeoutExpired:
        logger.error("FFmpeg检测超时")
        return False
    except Exception as e:
        logger.error(f"检测 FFmpeg 时出错: {e}")
        print(f"\n错误: 检测 FFmpeg 时出现问题: {e}")
        print("请确保 FFmpeg 已正确安装")
        return False

# 创建下载目录
def ensure_dirs():
    if not path.exists('./download/temp'):
        makedirs('./download/temp')
    if not path.exists('./download/failed'):
        makedirs('./download/failed')

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
            'gpu_mode': 'auto',  # 'auto', 'force_gpu', 'force_cpu'
            'convert_framerate': 'false',  # 新增：是否转换帧率
            'target_framerate': '30',       # 新增：目标帧率
            'max_retries': '3',             # 新增：最大重试次数
            'retry_delay': '5'              # 新增：重试延迟
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
            encoders_result = run(['ffmpeg', '-encoders'], stdout=PIPE, stderr=PIPE, text=True, timeout=15)
            encoders_output = encoders_result.stdout.lower()
            nvenc_available = 'h264_nvenc' in encoders_output
            logger.info(f"- NVENC编码器检测: {'✅ 可用' if nvenc_available else '❌ 不可用'}")
        except TimeoutExpired:
            nvenc_available = False
            logger.error("- NVENC编码器检测超时")
        except Exception as e:
            nvenc_available = False
            logger.error(f"- NVENC编码器检测失败: {e}")
        
        # 检查GPU是否可用 - 方法2：检查CUDA硬件加速
        hwaccels_output = ""
        try:
            hwaccels_result = run(['ffmpeg', '-hwaccels'], stdout=PIPE, stderr=PIPE, text=True, timeout=15)
            hwaccels_output = hwaccels_result.stdout.lower()
            cuda_available = 'cuda' in hwaccels_output
            logger.info(f"- CUDA硬件加速检测: {'✅ 可用' if cuda_available else '❌ 不可用'}")
        except TimeoutExpired:
            cuda_available = False
            logger.error("- CUDA硬件加速检测超时")
        except Exception as e:
            cuda_available = False
            logger.error(f"- CUDA硬件加速检测失败: {e}")
            
        # 检查GPU是否可用 - 方法3：使用nvidia-smi检查GPU
        nvidia_smi_available = False
        try:
            nvidia_smi_result = run(['nvidia-smi'], stdout=PIPE, stderr=PIPE, text=True, timeout=15)
            nvidia_smi_available = nvidia_smi_result.returncode == 0
            logger.info(f"- NVIDIA GPU检测: {'✅ 可用' if nvidia_smi_available else '❌ 不可用'}")
        except TimeoutExpired:
            nvidia_smi_available = False
            logger.info("- NVIDIA GPU检测超时")
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

def detect_video_framerate(video_path):
    try:
        cmd = [
            'ffprobe',
            '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=r_frame_rate',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            video_path
        ]
        result = run(cmd, stdout=PIPE, stderr=PIPE, text=True, timeout=10)
        if result.returncode == 0:
            # 帧率格式通常是 "30/1" 或 "30000/1001"
            frame_rate = result.stdout.strip()
            if not frame_rate:
                logger.warning("ffprobe未返回帧率信息")
                return None
                
            if '/' in frame_rate:
                num, den = frame_rate.split('/')
                try:
                    return round(float(num) / float(den), 1)
                except (ValueError, ZeroDivisionError):
                    logger.warning(f"无法解析帧率: {frame_rate}")
                    return None
            try:
                return float(frame_rate)
            except ValueError:
                logger.warning(f"无法转换帧率值: {frame_rate}")
                return None
        else:
            logger.error(f"检测视频帧率失败: {result.stderr}")
            return None
    except TimeoutExpired:
        logger.error("检测视频帧率超时")
        return None
    except Exception as e:
        logger.error(f"检测视频帧率时出错: {e}")
        return None

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

# 下载文件 - 增强错误处理和重试机制
async def download_file(url, save_path, desc, task_index, total_tasks, task_type, max_retries=3, retry_delay=5):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
    }
    
    from aiohttp import ClientSession, ClientPayloadError, ClientResponseError, ServerDisconnectedError
    progress_bar_key = f"download_{task_index}_{task_type}"
    
    # 添加重试机制
    retry_count = 0
    
    while retry_count < max_retries:
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
                else:
                    # 确保没有Range头（如果是第一次尝试）
                    if 'Range' in headers:
                        del headers['Range']
            
                # 下载文件
                mode = 'ab' if downloaded > 0 else 'wb'  # 如果已部分下载，则使用追加模式
                try:
                    async with session.get(url, headers=headers) as response:
                        # 检查响应状态
                        if response.status != 200 and response.status != 206:
                            raise Exception(f"HTTP错误: {response.status}")
                        
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
                except (ClientPayloadError, ServerDisconnectedError) as e:
                    # 捕获数据不完整的异常
                    logger.warning(f"数据接收不完整: {e}，将尝试恢复下载")
                    # 不抛出异常，继续处理，因为文件可能部分下载
                except Exception as e:
                    logger.error(f"下载过程中出错: {e}")
                    raise
            
            # 完成并清理资源
            progress_mgr.close_bar(progress_bar_key)
            
            # 检查文件完整性
            actual_size = path.getsize(save_path)
            if file_size > 0 and actual_size != file_size:
                logger.warning(f"文件大小不匹配，预期: {file_size}，实际: {actual_size}")
                
                # 如果差异大于2%，则删除文件并重试
                if actual_size < file_size * 0.98:
                    logger.info(f"删除不完整文件并重试: {save_path}")
                    try:
                        remove(save_path)
                    except:
                        pass
                    raise Exception(f"下载不完整，需要重新下载")
                else:
                    # 差异小于2%，可能服务器报告不准确，接受文件
                    logger.info(f"接受文件，差异在可接受范围内")
                    return True
            else:
                logger.info(f"✓ 完成下载: [{task_index}/{total_tasks}] {desc} {task_type}")
                return True
                
        except (ClientResponseError, ServerDisconnectedError) as e:
            logger.error(f"网络错误: {e}")
            retry_count += 1
            if retry_count < max_retries:
                logger.info(f"将在 {retry_delay} 秒后重试 ({retry_count}/{max_retries})")
                await asyncio_sleep(retry_delay)
                # 指数退避策略，增加重试间隔
                retry_delay *= 2
            else:
                logger.error(f"下载失败: [{task_index}] {desc} - 重试次数用尽")
                raise
        except Exception as e:
            logger.error(f"下载失败: [{task_index}] {desc} - {e}")
            retry_count += 1
            if retry_count < max_retries:
                logger.info(f"将在 {retry_delay} 秒后重试 ({retry_count}/{max_retries})")
                await asyncio_sleep(retry_delay)
                # 指数退避策略，增加重试间隔
                retry_delay *= 2
            else:
                logger.error(f"下载失败: [{task_index}] {desc} - 重试次数用尽")
                raise
    
    # 重试次数用尽
    logger.error(f"下载失败: [{task_index}] {desc} - 重试{max_retries}次后失败")
    progress_mgr.close_bar(progress_bar_key)
    raise Exception(f"重试{max_retries}次后下载失败")

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

# 构建FFmpeg命令行，支持编码转换
def build_ffmpeg_cmd(video_file, audio_file, output_file, use_gpu=False, width=1920, height=1080, 
                     original_codec="h264", convert_to_h265=False, convert_framerate=False, 
                     target_framerate=30, original_framerate=None, attempt=0):
    base_cmd = f'ffmpeg -y -i "{video_file}" -i "{audio_file}"'
    map_args = '-map 0:v:0 -map 1:a:0 -shortest'
    output_args = f'"{output_file}"'
    
    # 调试日志：显示传入的帧率参数
    logger.info(f"帧率转换设置: convert_framerate={convert_framerate}, target_framerate={target_framerate}, original_framerate={original_framerate}")

    # 确定是否应该转换帧率
    should_convert_framerate = False
    if convert_framerate and original_framerate is not None:
        # 只有原始帧率与目标帧率不同时才进行转换
        if abs(target_framerate - original_framerate) > 0.5:
            should_convert_framerate = True
            logger.info(f"原始帧率({original_framerate})和目标帧率({target_framerate})差异大于0.5，需要转换")
        else:
            logger.info(f"原始帧率{original_framerate}fps与目标帧率{target_framerate}fps相近，跳过转换")
    elif convert_framerate and original_framerate is None:
        # 无法检测原始帧率，但用户要求转换
        should_convert_framerate = True
        logger.info("无法检测原始帧率，但用户要求帧率转换，强制执行")
    
    # 构建滤镜链 - 确保帧率转换滤镜总是被添加（如果需要）
    vf_filters = []
    if should_convert_framerate:
        vf_filters.append(f"fps={target_framerate}")
        logger.info(f"添加帧率转换滤镜: {original_framerate or '未知'}fps → {target_framerate}fps")
    
    # 尝试3及以上方案包含缩放
    if attempt >= 3:
        vf_filters.append(f"scale={width}:{height}")
    
    # 组合滤镜
    vf_args = f'-vf "{",".join(vf_filters)}"' if vf_filters else ""
    
    # 根据原始编码和转换设置决定视频编码策略
    # 确定视频编码器
    if convert_to_h265:
        if use_gpu:
            # 尝试多种可能的GPU编码器
            gpu_encoders = ["hevc_nvenc", "h265_nvenc", "hevc_vaapi"]
            for encoder in gpu_encoders:
                if check_encoder_supported(encoder):
                    video_codec = encoder
                    break
            else:
                video_codec = "libx265"  # 回退到CPU编码
                logger.info("未找到可用的GPU编码器，回退到CPU编码")
        else:
            video_codec = "libx265"
    elif original_codec in ["h265", "hevc"]:
        # 如果原始就是H265，直接复制
        video_codec = "copy"
    else:
        # 其他编码保持原样
        video_codec = "copy"
    
    # 命令选项列表 - 优化转换方案
    cmd_options = [
        # 尝试0: 流复制（仅用于不需要转换的情况）
        f'{base_cmd} -c:v copy -c:a copy {map_args} {output_args}',
        
        # 尝试1: 使用检测到的编码策略（转换或复制）并添加滤镜
        f'{base_cmd} {vf_args} -c:v {video_codec} -c:a copy {map_args} {output_args}',
        
        # 尝试2: 添加硬件加速选项（如果使用GPU）并添加滤镜
        f'{base_cmd} {vf_args} -c:v {video_codec} -c:a copy {map_args} {output_args}' if not use_gpu else 
        f'{base_cmd} {vf_args} -hwaccel cuda -c:v {video_codec} -c:a copy {map_args} {output_args}',
        
        # 尝试3: 添加缩放（如果需要）
        f'{base_cmd} {vf_args} -c:v {video_codec} -c:a copy {map_args} {output_args}',
        
        # 尝试4: 使用更快的预设
        f'{base_cmd} {vf_args} -c:v {video_codec} -preset fast -c:a copy {map_args} {output_args}',
        
        # 尝试5: 强制使用CPU编码
        f'{base_cmd} {vf_args} -c:v libx265 -c:a copy {map_args} {output_args}'
    ]
    
    # 记录当前方案
    logger.info(f"方案 {attempt}: {cmd_options[attempt]}")
    
    # 根据尝试次数选择命令
    if attempt < len(cmd_options):
        return cmd_options[attempt]
    return cmd_options[-1]  # 返回最后一个命令作为保底

# 在编码器检测函数中添加更多日志
def check_encoder_supported(encoder_name):
    try:
        result = run(['ffmpeg', '-encoders'], stdout=PIPE, stderr=PIPE, text=True, timeout=15)
        if result.returncode == 0:
            if encoder_name in result.stdout:
                logger.info(f"编码器 {encoder_name} 可用")
                return True
            else:
                logger.warning(f"编码器 {encoder_name} 不可用")
                return False
        else:
            logger.error(f"获取编码器列表失败: {result.stderr}")
            return False
    except TimeoutExpired:
        logger.error(f"检测编码器 {encoder_name} 超时")
        return False
    except Exception as e:
        logger.error(f"检测编码器 {encoder_name} 时出错: {e}")
        return False

# 在FFmpeg中合成视频，改进错误处理和命令构建
def ffmpeg_merge(video_file, audio_file, output_file, title, index, total_count, duration, 
                 convert_to_h265=False, convert_framerate=False, 
                 target_framerate=30, original_framerate=None, attempt=0):
    # 确保变量有默认值
    width = 1920
    height = 1080
    original_codec = "h264"  # 设置默认值
    
    try:
        # 获取视频信息，包括编码、分辨率等
        try:
            # 使用更可靠的命令获取视频流信息
            cmd = [
                'ffprobe',
                '-v', 'error',
                '-select_streams', 'v:0',
                '-show_entries', 'stream=width,height,codec_name',
                '-of', 'json',
                video_file
            ]
            result = run(cmd, stdout=PIPE, stderr=PIPE, text=True, timeout=15)
            
            if result.returncode == 0:
                video_info = loads(result.stdout)
                
                # 找到第一个视频流
                for stream in video_info.get('streams', []):
                    if stream.get('codec_type') == 'video':
                        width = stream.get('width', 1920)
                        height = stream.get('height', 1080)
                        codec = stream.get('codec_name', 'h264')
                        
                        # 标准化编码名称
                        if codec in ["h264", "avc"]:
                            original_codec = "h264"
                        elif codec in ["h265", "hevc"]:
                            original_codec = "h265"
                        else:
                            original_codec = codec
                            
                        logger.info(f"检测到视频编码: {original_codec}, 分辨率: {width}x{height}")
                        break
            else:
                logger.error(f"获取视频信息失败: {result.stderr}")
        except TimeoutExpired:
            logger.error("获取视频信息超时")
        except Exception as e:
            logger.error(f"无法获取视频信息: {e}, 将使用默认参数")
        
        # 新增：如果不需要转换，直接使用流复制方案
        start_attempt = 0
        if convert_to_h265 and original_codec in ["h264", "avc"]:
            logger.info("需要H265转换，跳过流复制方案")
            start_attempt = 1  # 从转换方案开始尝试
        else:
            logger.info("不需要转换，使用流复制方案")
        
        # 创建进度条
        position = index % 10
        progress_bar_key = f"ffmpeg_{index}"
        encode_progress_bar = progress_mgr.create_bar(
            progress_bar_key,
            duration, 
            f'[{index}/{total_count}] {title} video [3/3]',
            position,
            unit='second'
        )
        
        success = False
        attempt = start_attempt
        max_attempts = 6
        
        # 尝试不同的合成方式
        while not success and attempt < max_attempts:
            try:
                # 构建命令行，传入原始编码和转换标志
                cmd_line = build_ffmpeg_cmd(
                    video_file, 
                    audio_file, 
                    output_file, 
                    use_gpu=NVIDIA_GPU_SUPPORTED, 
                    width=width, 
                    height=height,
                    original_codec=original_codec,
                    convert_to_h265=convert_to_h265,
                    convert_framerate=convert_framerate,
                    target_framerate=target_framerate,
                    original_framerate=original_framerate,
                    attempt=attempt
                )
                
                # 记录当前尝试 - 添加转换信息
                mode = "流复制" if attempt == 0 else "GPU" if NVIDIA_GPU_SUPPORTED and attempt < 5 else "CPU"
                conversion = ""
                if convert_to_h265 and original_codec in ["h264", "avc"]:
                    conversion = " (H265转换)"
                logger.info(f"尝试{mode}方案 {attempt+1}/{max_attempts} 合成视频 [{index}/{total_count}]{conversion}")
                
                # 重置进度条（如果不是第一次尝试）
                if attempt > start_attempt:
                    encode_progress_bar.reset()
                
                # 执行命令
                process = Popen(
                    cmd_line, 
                    stdout=PIPE, 
                    stderr=STDOUT, 
                    universal_newlines=True, 
                    encoding='utf-8',
                    shell=True,  # 确保命令行正确解析
                    bufsize=1
                )
                
                # 处理输出并更新进度条
                for line in process.stdout:
                    if line.startswith('size='):
                        time_length = parse_time_2_sec(line)
                        progress_mgr.update_bar(progress_bar_key, time_length - encode_progress_bar.n)
                
                # 检查命令执行结果
                return_code = process.wait()
                # 在合成成功的代码块中添加帧率验证
                if return_code == 0:
                    success = True
                    logger.info(f"{mode}方案 {attempt+1} 成功!")
                    
                    # 检测输出视频的实际编码
                    actual_codec = detect_video_codec(output_file)
                    if actual_codec:
                        logger.info(f"输出视频编码: {actual_codec}")
                        
                        # 检查是否成功转换为H265
                        if convert_to_h265 and original_codec in ["h264", "avc"]:
                            if "hevc" in actual_codec.lower() or "h265" in actual_codec.lower():
                                logger.info("✓ H265转换成功")
                            else:
                                logger.warning("H265转换失败！视频未转换为H265编码")
                                # 标记为失败以便尝试其他方案
                                success = False
                                attempt += 1
                                continue
                    # 检测输出视频的实际帧率
                    actual_framerate = detect_video_framerate(output_file)
                    if actual_framerate:
                        logger.info(f"输出视频帧率: {actual_framerate}fps")
                        
                        # 检查是否成功转换帧率
                        if convert_framerate:
                            # 计算帧率差异
                            framerate_diff = abs(actual_framerate - target_framerate)
                            
                            if framerate_diff < 0.5:  # 允许0.5fps的误差
                                logger.info("✓ 帧率转换成功")
                            else:
                                logger.warning(f"帧率转换未达到预期! 目标: {target_framerate}fps, 实际: {actual_framerate}fps")
                                
                                # 如果原始帧率可用，显示更多信息
                                if original_framerate:
                                    logger.info(f"原始帧率: {original_framerate}fps, 目标帧率: {target_framerate}fps")
                    else:
                        logger.warning("无法检测输出视频帧率")
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

# 清理临时目录
def cleanup_temp_dir():
    temp_dir = './download/temp'
    if path.exists(temp_dir):
        try:
            for filename in listdir(temp_dir):
                file_path = path.join(temp_dir, filename)
                try:
                    if path.isfile(file_path):
                        remove(file_path)
                    elif path.isdir(file_path):
                        shutil.rmtree(file_path)
                except Exception as e:
                    logger.warning(f"无法删除临时文件 {file_path}: {e}")
        except Exception as e:
            logger.error(f"清理临时目录时出错: {e}")

# 处理单个视频的下载和合成 - 优化为一节课一节课处理
async def process_episode(ep, position_index, total_count, semaphore, course_folder, 
                          original_index, convert_to_h265=False, convert_framerate=False, 
                          target_framerate=30, max_retries=3, retry_delay=5):
    try:
        async with semaphore:
            # 基础参数配置
            ep_id = ep.get_epid()
            original_title = (await ep.get_meta())['title']
            # 替换非法字符
            safe_title = sanitize_filename(original_title)
            title = format_title(safe_title)  # 格式化标题用于显示
            
            # 获取音频和视频的链接，并设置本地保存的文件名
            filename_prefix = f"{original_index}_{uuid4().hex}"
            download_url_data = await ep.get_download_url()
            
            # 解析下载链接
            detector = video.VideoDownloadURLDataDetecter(data=download_url_data)
            streams = detector.detect_best_streams()
            
            # 在这里定义 audio_file 和 video_file
            audio_file = f"./download/temp/{filename_prefix}_audio.m4s"
            video_file = f"./download/temp/{filename_prefix}_video.m4s"
            
            # 使用课程文件夹保存文件，使用原始序号作为文件名前缀
            output_file = f"./download/{course_folder}/{original_index:03d}_{safe_title}.mp4"
            
            # 确保课程文件夹存在
            makedirs(f"./download/{course_folder}", exist_ok=True)
            
            # 下载音频和视频
            await download_file(streams[1].url, audio_file, title, position_index, total_count, "audio [1/3]", max_retries, retry_delay)
            await download_file(streams[0].url, video_file, title, position_index, total_count, "video [2/3]", max_retries, retry_delay)
            
            # 验证下载的文件是否存在且大小大于0
            if not path.exists(audio_file) or path.getsize(audio_file) == 0:
                raise Exception(f"音频文件下载失败或大小为0: {audio_file}")
            if not path.exists(video_file) or path.getsize(video_file) == 0:
                raise Exception(f"视频文件下载失败或大小为0: {video_file}")
            
            # 获取视频时长用于进度条
            video_meta = await ep.get_meta()
            duration = video_meta.get('duration', 0)

            # 获取视频原始帧率
            original_framerate = None
            if convert_framerate:
                try:
                    # 使用新函数确保正确检测帧率
                    original_framerate = detect_video_framerate(video_file)
                    if original_framerate:
                        logger.info(f"检测到视频原始帧率: {original_framerate}fps")
                    else:
                        logger.warning("无法检测视频帧率，使用默认值60fps")
                        original_framerate = 60.0  # 设置合理的默认值
                except Exception as e:
                    logger.error(f"检测视频帧率失败: {e}")
                    original_framerate = 60.0  # 设置合理的默认值
            
            # 立即处理合成任务
            result = ffmpeg_merge(
                video_file, 
                audio_file, 
                output_file, 
                title, 
                position_index, 
                total_count, 
                duration,
                convert_to_h265,  # 传入转换标志
                convert_framerate,  # 帧率转换标志
                target_framerate,  # 目标帧率
                original_framerate  # 原始帧率
            )
            
            return {"success": result, "position_index": position_index, "original_index": original_index, "episode": ep}
            
    except Exception as e:
        logger.error(f"处理视频 {position_index} 时出错: {e}")
        # 尝试清理可能的临时文件
        try:
            # 使用 locals().get() 安全地检查变量是否存在
            if 'audio_file' in locals() and path.exists(audio_file):
                remove(audio_file)
            if 'video_file' in locals() and path.exists(video_file):
                remove(video_file)
        except Exception:
            pass
        
        # 保存失败信息
        error_info = {
            "position_index": position_index,
            "original_index": original_index,
            "episode": ep,
            "error": str(e)
        }
        with open(f"./download/failed/{original_index:03d}_error.json", "w", encoding="utf-8") as f:
            dumps(error_info, f, indent=2, ensure_ascii=False)
            
        return {"success": False, "position_index": position_index, "original_index": original_index, "episode": ep, "error": str(e)}

# 主程序 - 添加配置文件支持和改进错误处理
async def main():
    try:
        ensure_dirs()  # 确保目录存在
        cleanup_temp_dir()  # 清理临时目录
        
        # 检查FFmpeg是否已安装
        if not check_ffmpeg():
            print("\n程序无法继续，请安装FFmpeg后重试。")
            return
            
        # 加载配置
        config = load_config()
        default_convert_to_h265 = config.getboolean('General', 'convert_to_h265', fallback=False)
        default_concurrent_downloads = config.getint('General', 'concurrent_downloads', fallback=2)
        default_concurrent_ffmpeg = config.getint('General', 'concurrent_ffmpeg', fallback=1)
        gpu_mode = config.get('General', 'gpu_mode', fallback='auto')
        max_retries = config.getint('General', 'max_retries', fallback=3)
        retry_delay = config.getint('General', 'retry_delay', fallback=5)

        # 询问用户是否开启帧率转换
        default_convert_framerate = config.getboolean('General', 'convert_framerate', fallback=False)
        default_target_framerate = config.getint('General', 'target_framerate', fallback=30)
        
        print("\n== 帧率转换设置 ==")
        print("开启帧率转换可以调整视频的流畅度")
        print("常见帧率: 24 (电影感), 30 (标准), 60 (流畅)")
        print("1. 开启帧率转换")
        print("2. 保持原始帧率")
        choice = input(f"请选择 (默认: {'开启' if default_convert_framerate else '保持'}): ").strip() or None
        
        convert_framerate = default_convert_framerate
        target_framerate = default_target_framerate
        
        if choice == '1':
            convert_framerate = True
            try:
                user_input = input(f"请输入目标帧率 (默认: {default_target_framerate}): ").strip()
                if user_input:
                    target_framerate = int(user_input)
                    if target_framerate < 1 or target_framerate > 120:
                        logger.warning(f"无效的帧率 {target_framerate}，使用默认值 {default_target_framerate}")
                        target_framerate = default_target_framerate
            except ValueError:
                logger.warning(f"输入无效，使用默认帧率 {default_target_framerate}")
                target_framerate = default_target_framerate

        # 询问用户是否开启H265转换
        print("\n== H265转换设置 ==")
        print("开启H265转换可以减小文件大小（约30-50%），但会增加处理时间")
        print("注意：只有原始编码为H264的视频会被转换")
        print("      原始就是H265的视频将保持原样")
        print("1. 开启H265转换（推荐）")
        print("2. 保持原始编码")
        choice = input(f"请选择 (默认: {'开启' if default_convert_to_h265 else '保持'}): ").strip() or None
        
        convert_to_h265 = default_convert_to_h265
        if choice == '1':
            convert_to_h265 = True
        elif choice == '2':
            convert_to_h265 = False

        # 在main函数中修改H265检测部分
        if convert_to_h265:
            logger.info("已启用H265转换")
            hevc_supported = check_h265_support(NVIDIA_GPU_SUPPORTED)
            
            if not hevc_supported:
                logger.warning("当前系统不支持H265编码，将使用H264")
                convert_to_h265 = False
            else:
                logger.info("系统支持H265编码")
        
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
                
                # 显示所有集数供用户选择
                print("\n课程包含以下集数:")
                for i, ep in enumerate(episodes, 1):
                    meta = await ep.get_meta()
                    title = meta.get('title', f'第{i}集')
                    print(f"[{i}] {title}")
                
                # 询问用户下载范围
                print("\n请选择要下载的集数:")
                print("1. 全部下载")
                print("2. 下载指定集数")
                print("3. 下载范围集数")
                choice = input("请输入选项 (1-3): ").strip()
                
                selected_episodes = []
                
                if choice == '1':
                    # 下载全部
                    selected_episodes = [(i, ep) for i, ep in enumerate(episodes, 1)]
                elif choice == '2':
                    # 下载指定集数
                    episodes_input = input("请输入要下载的集数 (用逗号分隔, 如: 1,3,5): ").strip()
                    if episodes_input:
                        try:
                            episode_nums = [int(num.strip()) for num in episodes_input.split(',')]
                            for num in episode_nums:
                                if 1 <= num <= len(episodes):
                                    selected_episodes.append((num, episodes[num-1]))
                                else:
                                    print(f"忽略无效集数: {num}")
                        except ValueError:
                            print("输入格式错误，请使用数字和逗号分隔")
                elif choice == '3':
                    # 下载范围集数
                    start_input = input("请输入起始集数: ").strip()
                    end_input = input("请输入结束集数: ").strip()
                    try:
                        start = int(start_input)
                        end = int(end_input)
                        if 1 <= start <= len(episodes) and 1 <= end <= len(episodes) and start <= end:
                            for num in range(start, end + 1):
                                selected_episodes.append((num, episodes[num-1]))
                        else:
                            print("无效的范围，将下载全部")
                            selected_episodes = [(i, ep) for i, ep in enumerate(episodes, 1)]
                    except ValueError:
                        print("输入格式错误，将下载全部")
                        selected_episodes = [(i, ep) for i, ep in enumerate(episodes, 1)]
                else:
                    print("无效选择，将下载全部")
                    selected_episodes = [(i, ep) for i, ep in enumerate(episodes, 1)]
                
                if not selected_episodes:
                    print("没有选择任何集数，程序退出")
                    return
                
                print(f"已选择下载 {len(selected_episodes)} 集视频")
                
                # 询问用户并行下载数量，使用配置文件默认值
                concurrent_downloads = default_concurrent_downloads
                try:
                    user_input = input(f'请输入并行下载的数量（默认为{default_concurrent_downloads}）: ').strip()
                    if user_input:
                        concurrent_downloads = int(user_input)
                        if concurrent_downloads < 1:
                            concurrent_downloads = 1
                        elif concurrent_downloads > len(selected_episodes):
                            concurrent_downloads = len(selected_episodes)
                except ValueError:
                    logger.warning(f"输入无效，使用默认值{default_concurrent_downloads}")
                    concurrent_downloads = default_concurrent_downloads
                
                # 创建信号量以限制并发下载数
                semaphore = Semaphore(concurrent_downloads)
                
                # 创建用于ffmpeg合成的线程池
                tasks = []
                # 在创建下载任务时传入帧率转换参数
                for i, (original_index, ep) in enumerate(selected_episodes, 1):
                    task = create_task(process_episode(
                        ep, 
                        i,  # 当前任务在用户选择列表中的位置
                        len(selected_episodes), 
                        semaphore, 
                        course_folder,
                        original_index,  # 原始序号用于文件名
                        convert_to_h265,  # H265转换标志
                        convert_framerate,  # 帧率转换标志
                        target_framerate,   # 目标帧率
                        max_retries,       # 最大重试次数
                        retry_delay        # 重试延迟
                    ))
                    tasks.append(task)
                
                # 等待所有任务完成
                results = await gather(*tasks)
                
                # 统计下载结果
                success_count = sum(1 for r in results if r.get("success", False))
                failed_count = len(selected_episodes) - success_count
                print(f"\n下载完成，成功: {success_count}, 失败: {failed_count}")
                
                # 显示失败的任务
                if failed_count > 0:
                    print("\n失败的任务:")
                    for result in results:
                        if not result.get("success", False):
                            meta = await result['episode'].get_meta()
                            title = meta.get('title', f'第{result["original_index"]}集')
                            print(f"  - [{result['original_index']}] {title}: {result.get('error', '未知错误')}")
                            print(f"    错误详情已保存到: ./download/failed/{result['original_index']:03d}_error.json")
                
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
        cleanup_temp_dir()  # 最后清理临时目录
        
    except Exception as e:
        logger.error(f"程序执行出错: {e}")
        from traceback import print_exc
        print_exc()
        # 确保清理资源
        progress_mgr.close_all()
        cleanup_temp_dir()

if __name__ == "__main__":
    # 运行主程序
    try:
        asyncio_run(main())
    except KeyboardInterrupt:
        logger.warning("\n程序被用户中断")
        progress_mgr.close_all()
        cleanup_temp_dir()
    except Exception as e:
        logger.error(f"程序异常退出: {e}")
        progress_mgr.close_all()
        cleanup_temp_dir()