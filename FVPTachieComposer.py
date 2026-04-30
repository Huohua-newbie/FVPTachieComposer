import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
import struct
import zlib
from PIL import Image, ImageTk
import os
import ctypes



# ---------- HZC 文件解析与转换 ----------
def parse_hzc_header(header_bytes):
	"""
	解析 HZC 文件头（44 字节）
	返回字典包含关键信息
	"""
	if len(header_bytes) < 44:
		raise ValueError("文件头不足 44 字节")

	magic = header_bytes[0:4].decode('ascii', errors='ignore')
	if magic != "hzc1":
		print(f"警告: 魔数不是 'hzc1'，实际为 {magic}")

	original_size = struct.unpack('<I', header_bytes[4:8])[0]
	image_type = struct.unpack('<H', header_bytes[18:20])[0]
	width = struct.unpack('<H', header_bytes[20:22])[0]
	height = struct.unpack('<H', header_bytes[22:24])[0]
	offset_x = struct.unpack('<H', header_bytes[24:26])[0]
	offset_y = struct.unpack('<H', header_bytes[26:28])[0]
	diff = struct.unpack('<I', header_bytes[32:36])[0]
	frame_count = diff if image_type == 2 else 1

	return {
		'magic': magic,
		'original_size': original_size,
		'image_type': image_type,
		'width': width,
		'height': height,
		'frame_count': frame_count,
		'offset_x': offset_x,
		'offset_y': offset_y,
	}

def transform_bytes_bytearray(data):
	"""
	字节变换函数（仅用于多帧 HZC）：每4个字节一组，交换位置0和2
	"""
	byte_arr = bytearray(data)
	for i in range(0, len(byte_arr), 4):
		if i + 3 < len(byte_arr):
			byte_arr[i], byte_arr[i+2] = byte_arr[i+2], byte_arr[i]
	return bytes(byte_arr)

def convert_hzc_data(hzc_data, original_filename, base_output_dir):
	"""
	将 HZC 二进制数据转换为 PNG 图片
	:param hzc_data: 完整的 HZC 文件数据
	:param original_filename: 原始文件名（不含扩展名）
	:param base_output_dir: 基础输出目录（例如 bin 文件名去掉后缀的目录）
	:return: 字典包含处理信息
	"""
	if len(hzc_data) < 44:
		print(f"错误: 文件数据过小，不是有效的 HZC 文件")
		return None

	header = parse_hzc_header(hzc_data[:44])
	image_type = header['image_type']
	width = header['width']
	height = header['height']
	frame_count = header['frame_count']
	offset_x = header['offset_x'] if header['image_type'] == 2 else None
	offset_y = header['offset_y'] if header['image_type'] == 2 else None

	# 确定输出文件夹
	is_emotion = original_filename.endswith('_表情')
	out_dir = Path(base_output_dir) / original_filename  # 统一以完整文件名作为文件夹名
	out_dir.mkdir(parents=True, exist_ok=True)

	# 解压数据
	compressed_data = hzc_data[44:]
	try:
		decompressed = zlib.decompress(compressed_data)
	except zlib.error as e:
		print(f"解压失败 {original_filename}: {e}")
		return None

	saved_paths = []

	if image_type == 2:  # 多帧（表情部件通常为此类）
		# 应用字节变换
		transformed = transform_bytes_bytearray(decompressed)
		bytes_per_frame = width * height * 4  # 每帧 RGBA 大小
		for i in range(frame_count):
			start = i * bytes_per_frame
			frame_data = transformed[start:start+bytes_per_frame]
			if len(frame_data) < bytes_per_frame:
				print(f"警告: 帧 {i} 数据不足，跳过")
				continue
			img = Image.frombytes('RGBA', (width, height), frame_data)
			out_filename = f"{original_filename}_{i:03d}.png"
			out_path = out_dir / out_filename
			img.save(out_path, 'PNG')
			saved_paths.append(str(out_path))
		print(f"已转换多帧: {original_filename} -> {out_dir} (共 {frame_count} 帧)")

	else:  # 单图 (image_type 0 或 1)
		if image_type == 0:  # 24位 BGR
			bytes_per_pixel = 3
			mode = 'RGB'
			expected = width * height * bytes_per_pixel
			if len(decompressed) != expected:
				print(f"警告: 数据大小不匹配，预期 {expected}，实际 {len(decompressed)}")
			img = Image.frombytes(mode, (width, height), decompressed)
			b, g, r = img.split()
			img = Image.merge("RGB", (r, g, b))
		else:  # 假设为 1 (32位 BGRA) 或其他
			bytes_per_pixel = 4
			mode = 'RGBA'
			img = Image.frombytes(mode, (width, height), decompressed)
			b, g, r, a = img.split()
			img = Image.merge("RGBA", (r, g, b, a))

		out_filename = f"{original_filename}.png"
		out_path = out_dir / out_filename
		img.save(out_path, 'PNG')
		saved_paths.append(str(out_path))
		print(f"已转换单图: {original_filename} -> {out_path}")

	# 返回处理信息
	return {
		'is_emotion': is_emotion,
		'base_dir': out_dir,
		'offset_x': offset_x,
		'offset_y': offset_y,
		'frame_count': frame_count,
		'saved_files': saved_paths
	}

# ---------- bin 文件解析 ----------
def parse_bin_info(input_file: str):
	"""
	解析.bin文件，返回文件信息列表。
	每个元素为字典：{'filename': 文件名（不含扩展名）, 'offset': 绝对偏移, 'size': 大小, 'type': 类型}
	"""
	with open(input_file, 'rb') as f:
		header = f.read(8)
		if len(header) != 8:
			raise ValueError("文件头不完整")
		x, y = struct.unpack('<II', header)  # 文件数，文件名总长度

		entries = []  # (rel_offset, abs_offset, size)
		for _ in range(x):
			entry_data = f.read(12)
			if len(entry_data) != 12:
				raise ValueError("文件信息表不完整")
			rel_offset, abs_offset, size = struct.unpack('<III', entry_data)
			entries.append((rel_offset, abs_offset, size))

		filenames_data = f.read(y)
		if len(filenames_data) != y:
			raise ValueError("文件名区域长度不符")

		# 解析所有文件名
		filenames = []
		for rel_offset, _, _ in entries:
			if rel_offset >= y:
				raise ValueError(f"无效的文件名偏移：{rel_offset}")
			start = rel_offset
			end = filenames_data.find(b'\x00', start)
			if end == -1:
				end = y
			filename_bytes = filenames_data[start:end]
			try:
				filename = filename_bytes.decode('shift-jis')
			except UnicodeDecodeError:
				filename = filename_bytes.decode('shift-jis', errors='replace')
			filenames.append(filename)

		# 预检测每个文件的类型（读取前4字节）
		file_types = []
		for idx, (_, abs_offset, size) in enumerate(entries):
			f.seek(abs_offset)
			header_bytes = f.read(4) if size >= 4 else b''
			if header_bytes == b'hzc1':
				typ = 'hzc'
			elif header_bytes == b'OggS':
				typ = 'ogg'
			elif header_bytes == b'RIFF':
				typ = 'wav'
			else:
				typ = 'bin'
			file_types.append(typ)

		# 构建返回列表
		file_infos = []
		for i, filename in enumerate(filenames):
			file_infos.append({
				'filename': filename,
				'offset': entries[i][1],
				'size': entries[i][2],
				'type': file_types[i]
			})
		return file_infos

# ---------- 层级细分选择 ----------
def interactive_filter_by_parts(file_infos):
	"""
	根据文件名下划线分割部分进行多级筛选。
	返回筛选后的 file_infos 列表。
	"""
	current_list = file_infos
	level = 2  # 从索引2开始（因为索引0="CHR"，索引1=角色名，已固定）

	while True:
		# 获取当前层级的所有可能值（只考虑文件名分割后长度 > level 的文件）
		values = set()
		for info in current_list:
			parts = info['filename'].split('_')
			if len(parts) > level:
				values.add(parts[level])
		sorted_values = sorted(values)
		if not sorted_values:
			print("没有更多细分层级，将处理当前所有文件。")
			break

		print(f"\n当前层级（第{level}部分）的可选值：")
		for i, val in enumerate(sorted_values, 1):
			print(f"{i}. {val}")
		print("0. 全选（处理当前所有文件）")

		choice = input("请选择序号：").strip()
		if choice == '0':
			break
		try:
			idx = int(choice) - 1
			if 0 <= idx < len(sorted_values):
				selected_val = sorted_values[idx]
				# 筛选出文件该部分等于 selected_val 的文件（只保留长度 > level 且值匹配的）
				new_list = [
					info for info in current_list
					if len(info['filename'].split('_')) > level and info['filename'].split('_')[level] == selected_val
				]
				if not new_list:
					print("选择后无文件，将保留原列表。")
					# 实际上不应发生，因为 selected_val 是从当前列表提取的
				else:
					current_list = new_list
				level += 1
				continue
			else:
				print(f"序号超出范围，应为 0~{len(sorted_values)}")
		except ValueError:
			print("输入无效，请输入数字。")
		# 如果输入无效，重新循环
	return current_list

# ---------- 提取并转换符合条件的文件 ----------
def extract_and_convert_by_condition(input_file, file_infos, output_dir, condition_func):
	"""
	从 bin 文件中读取符合条件的数据，并立即转换为 PNG
	返回转换后的信息列表（每个元素是 convert_hzc_data 的返回值）
	"""
	results = []
	with open(input_file, 'rb') as f:
		for info in file_infos:
			if not condition_func(info):
				continue
			f.seek(info['offset'])
			data = f.read(info['size'])
			if len(data) != info['size']:
				raise ValueError(f"文件 {info['filename']} 数据不完整")
			conv_info = convert_hzc_data(data, info['filename'], output_dir)
			if conv_info:
				results.append(conv_info)
	return results

# ---------- 差分合成 ----------
def compose_differentials(base_output_dir, converted_infos):
	"""
	根据转换后的信息，对每个底图及其对应的表情部件进行差分合成
	:param base_output_dir: 基础输出目录（同 convert_hzc_data 中的 base_output_dir）
	:param converted_infos: extract_and_convert_by_condition 返回的列表
	"""
	# 建立从部件文件夹路径到对应信息的映射
	emotion_map = {}
	base_infos = []  # 底图信息列表
	for info in converted_infos:
		if info['is_emotion']:
			emotion_map[str(info['base_dir'])] = info
		else:
			base_infos.append(info)

	for base_info in base_infos:
		base_dir = base_info['base_dir']
		base_filename = base_dir.name

		# 底图 PNG 路径
		base_img_path = base_dir / f"{base_filename}.png"
		if not base_img_path.exists():
			print(f"警告: 底图文件不存在 {base_img_path}")
			continue

		# 对应的表情部件文件夹（与底图同父目录，名为 base_filename + '_表情'）
		emotion_folder = base_dir.parent / (base_filename + "_表情")
		if str(emotion_folder) not in emotion_map:
			continue

		emotion_info = emotion_map[str(emotion_folder)]
		offset_x = emotion_info['offset_x']
		offset_y = emotion_info['offset_y']
		if offset_x is None or offset_y is None:
			print(f"警告: 部件文件夹 {emotion_folder} 缺少偏移信息，跳过")
			continue

		# 创建输出子文件夹 diff
		diff_dir = base_dir / "diff"
		diff_dir.mkdir(exist_ok=True)

		# 打开底图
		base_img = Image.open(base_img_path).convert("RGBA")

		# 遍历部件文件夹中的所有 PNG 文件（按文件名排序以确保顺序稳定）
		for png_path in sorted(emotion_folder.glob("*.png")):
			comp_img = Image.open(png_path).convert("RGBA")
			w, h = comp_img.size

			# 合成
			result = base_img.copy()
			paste_x, paste_y = offset_x, offset_y

			# 计算有效重叠区域
			overlap = (
				max(0, paste_x),
				max(0, paste_y),
				min(base_img.width, paste_x + w),
				min(base_img.height, paste_y + h)
			)
			if overlap[0] < overlap[2] and overlap[1] < overlap[3]:
				comp_crop = (
					overlap[0] - paste_x,
					overlap[1] - paste_y,
					overlap[2] - paste_x,
					overlap[3] - paste_y
				)
				comp_region = comp_img.crop(comp_crop)
				base_region = result.crop(overlap)
				blended = Image.alpha_composite(base_region, comp_region)
				result.paste(blended, overlap)

			# 保存结果
			out_filename = f"diff_{png_path.name}"
			out_path = diff_dir / out_filename
			result.save(out_path)
			print(f"已合成: {out_path}")

	print("差分合成完成！")

# ---------- 扩展解析：获取 HZC 头部信息 ----------
def parse_hzc_header_from_bytes(header_bytes):
	"""从44字节头部解析信息"""
	if len(header_bytes) < 44:
		return None
	magic = header_bytes[0:4].decode('ascii', errors='ignore')
	if magic != "hzc1":
		return None
	image_type = struct.unpack('<H', header_bytes[18:20])[0]
	width = struct.unpack('<H', header_bytes[20:22])[0]
	height = struct.unpack('<H', header_bytes[22:24])[0]
	offset_x = struct.unpack('<H', header_bytes[24:26])[0]
	offset_y = struct.unpack('<H', header_bytes[26:28])[0]
	diff = struct.unpack('<I', header_bytes[32:36])[0]
	frame_count = diff if image_type == 2 else 1
	return {
		'image_type': image_type,
		'width': width,
		'height': height,
		'offset_x': offset_x,
		'offset_y': offset_y,
		'frame_count': frame_count
	}

def parse_bin_info_extended(input_file):
	"""调用原 parse_bin_info 并补充 HZC 头部信息"""
	base_infos = parse_bin_info(input_file)  # 原函数返回列表，每个元素含 filename, offset, size, type
	with open(input_file, 'rb') as f:
		for info in base_infos:
			if info['type'] == 'hzc':
				f.seek(info['offset'])
				header_data = f.read(44)
				header_info = parse_hzc_header_from_bytes(header_data)
				if header_info:
					info.update(header_info)
				else:
					info.update({'image_type': 0, 'width': 0, 'height': 0, 'offset_x': 0, 'offset_y': 0, 'frame_count': 1})
			else:
				info.update({'image_type': None, 'width': 0, 'height': 0, 'offset_x': 0, 'offset_y': 0, 'frame_count': 1})
	return base_infos

# ---------- 将 HZC 数据转换为 PIL 图像列表 ----------
def hzc_data_to_pil_list(hzc_data, header_info):
	"""
	根据 HZC 数据和头部信息，返回 PIL Image 列表
	header_info 应包含 image_type, width, height, frame_count
	"""
	if len(hzc_data) < 44:
		return []
	compressed = hzc_data[44:]
	try:
		decompressed = zlib.decompress(compressed)
	except zlib.error:
		return []
	image_type = header_info['image_type']
	width = header_info['width']
	height = header_info['height']
	frame_count = header_info['frame_count']

	if image_type == 2:  # 多帧
		transformed = transform_bytes_bytearray(decompressed)
		bytes_per_frame = width * height * 4
		images = []
		for i in range(frame_count):
			start = i * bytes_per_frame
			frame_data = transformed[start:start+bytes_per_frame]
			if len(frame_data) < bytes_per_frame:
				break
			img = Image.frombytes('RGBA', (width, height), frame_data)
			images.append(img)
		return images
	else:
		if image_type == 0:  # 24位
			bytes_per_pixel = 3
			mode = 'RGB'
			expected = width * height * bytes_per_pixel
			if len(decompressed) != expected:
				decompressed = decompressed[:expected]
			img = Image.frombytes(mode, (width, height), decompressed)
			b, g, r = img.split()
			img = Image.merge("RGB", (r, g, b))
		else:  # 32位
			bytes_per_pixel = 4
			mode = 'RGBA'
			expected = width * height * bytes_per_pixel
			if len(decompressed) != expected:
				decompressed = decompressed[:expected]
			img = Image.frombytes(mode, (width, height), decompressed)
			b, g, r, a = img.split()
			img = Image.merge("RGBA", (r, g, b, a))
		return [img]

# ---------- 合成差分图像 ----------
def compose_preview(base_img, part_img, offset_x, offset_y):
	"""将部件图像合成到底图上，返回合成后的 PIL Image"""
	base = base_img.convert('RGBA')
	part = part_img.convert('RGBA')
	result = base.copy()
	w, h = part.size
	paste_x, paste_y = offset_x, offset_y
	# 计算有效重叠区域
	overlap = (
		max(0, paste_x),
		max(0, paste_y),
		min(base.width, paste_x + w),
		min(base.height, paste_y + h)
	)
	if overlap[0] < overlap[2] and overlap[1] < overlap[3]:
		comp_crop = (
			overlap[0] - paste_x,
			overlap[1] - paste_y,
			overlap[2] - paste_x,
			overlap[3] - paste_y
		)
		comp_region = part.crop(comp_crop)
		base_region = result.crop(overlap)
		blended = Image.alpha_composite(base_region, comp_region)
		result.paste(blended, overlap)
	return result

# ---------- GUI 主类 ----------
class HZCGUI:
	def _build_custom_title_bar(self):
		"""自绘窗口标题栏，彻底替代系统标题栏"""
		self._drag_start_x = 0
		self._drag_start_y = 0
		self._is_maximized = False

		title_bar = tk.Frame(self.root, bg=self.theme_colors["panel_alt"], height=36, bd=0, highlightthickness=0)
		title_bar.pack(fill=tk.X, side=tk.TOP)
		title_bar.pack_propagate(False)
		self.title_bar = title_bar

		title_label = tk.Label(
			title_bar,
			text="FVP Tachie Composer",
			bg=self.theme_colors["panel_alt"],
			fg=self.theme_colors["fg"],
			font=("Segoe UI Semibold", 10)
		)
		title_label.pack(side=tk.LEFT, padx=12)

		# 允许拖拽窗口（标题栏和标题文本）
		for widget in (title_bar, title_label):
			widget.bind("<ButtonPress-1>", self._start_window_drag)
			widget.bind("<B1-Motion>", self._on_window_drag)
			widget.bind("<Double-Button-1>", self._toggle_maximize_restore)

		btn_wrap = tk.Frame(title_bar, bg=self.theme_colors["panel_alt"], bd=0, highlightthickness=0)
		btn_wrap.pack(side=tk.RIGHT, fill=tk.Y)

		btn_style = {
			"bg": self.theme_colors["panel_alt"],
			"fg": self.theme_colors["fg"],
			"bd": 0,
			"highlightthickness": 0,
			"font": ("Segoe UI", 10),
			"width": 4,
			"activebackground": "#323a4e",
			"activeforeground": "#ffffff",
			"cursor": "hand2",
		}
		close_style = dict(btn_style)
		close_style["activebackground"] = "#c24040"

		self.btn_min = tk.Button(btn_wrap, text="—", command=self._minimize_window, **btn_style)
		self.btn_min.pack(side=tk.LEFT, fill=tk.Y)
		self.btn_max = tk.Button(btn_wrap, text="□", command=self._toggle_maximize_restore, **btn_style)
		self.btn_max.pack(side=tk.LEFT, fill=tk.Y)
		self.btn_close = tk.Button(btn_wrap, text="✕", command=self.root.destroy, **close_style)
		self.btn_close.pack(side=tk.LEFT, fill=tk.Y)

	def _start_window_drag(self, event):
		if self._is_maximized:
			return
		self._drag_start_x = event.x_root - self.root.winfo_x()
		self._drag_start_y = event.y_root - self.root.winfo_y()

	def _on_window_drag(self, event):
		if self._is_maximized:
			return
		x = event.x_root - self._drag_start_x
		y = event.y_root - self._drag_start_y
		self.root.geometry(f"+{x}+{y}")

	def _toggle_maximize_restore(self, event=None):
		try:
			if self._is_maximized:
				self.root.state("normal")
				self.btn_max.config(text="□")
				self._is_maximized = False
			else:
				self.root.state("zoomed")
				self.btn_max.config(text="❐")
				self._is_maximized = True
		except tk.TclError:
			# 某些环境不支持 zoomed，退化为原始状态
			self.root.state("normal")
			self._is_maximized = False

	def _minimize_window(self):
		# 无边框窗口最小化需要临时恢复系统装饰
		self.root.overrideredirect(False)
		self.root.iconify()
		self.root.bind("<Map>", self._restore_borderless_after_map, add="+")

	def _restore_borderless_after_map(self, event=None):
		self.root.unbind("<Map>")
		self.root.overrideredirect(True)
		if self._is_maximized:
			self.root.state("zoomed")
		else:
			self.root.state("normal")

	def _enable_dark_title_bar(self):
		"""使用 Windows DWM API 尝试启用暗色标题栏"""
		if os.name != "nt":
			return
		try:
			hwnd = self.root.winfo_id()
			value = ctypes.c_int(1)
			# Windows 10/11 常用暗色标题栏属性
			DWMWA_USE_IMMERSIVE_DARK_MODE = 20
			ctypes.windll.dwmapi.DwmSetWindowAttribute(
				ctypes.c_void_p(hwnd),
				ctypes.c_uint(DWMWA_USE_IMMERSIVE_DARK_MODE),
				ctypes.byref(value),
				ctypes.sizeof(value),
			)
		except Exception:
			# 仅外观增强失败，不影响功能
			pass

	def _configure_styles(self):
		"""配置更现代化的 ttk 样式"""
		style = ttk.Style()
		try:
			style.theme_use("clam")
		except tk.TclError:
			pass

		# 现代暗色风格
		bg = "#161a22"
		panel = "#1f2430"
		panel_alt = "#252c3b"
		fg = "#ecf0fa"
		muted = "#aab3c9"
		accent = "#5f88ff"
		accent_hover = "#7ca0ff"
		border = "#333c52"
		self.theme_colors = {
			"bg": bg,
			"panel": panel,
			"panel_alt": panel_alt,
			"fg": fg,
			"muted": muted,
			"accent": accent,
			"accent_hover": accent_hover,
			"border": border
		}

		self.root.configure(bg=bg)
		style.configure(".", background=bg, foreground=fg)
		style.configure("TFrame", background=bg)
		style.configure("Card.TFrame", background=panel)
		style.configure("Toolbar.TFrame", background=panel_alt)
		style.configure("TLabel", background=bg, foreground=fg, font=("Segoe UI", 10))
		style.configure("Muted.TLabel", background=panel_alt, foreground=muted, font=("Segoe UI", 9))
		style.configure("Header.TLabel", background=panel_alt, foreground=fg, font=("Segoe UI Semibold", 11))
		style.configure(
			"TLabelframe",
			background=panel,
			bordercolor=border,
			lightcolor=border,
			darkcolor=border,
			borderwidth=1,
			relief="solid"
		)
		style.configure("TLabelframe.Label", background=panel, foreground=fg, font=("Segoe UI Semibold", 10))
		style.configure("TButton", font=("Segoe UI", 10), padding=(12, 7), relief="flat", borderwidth=0)
		style.map(
			"TButton",
			background=[("active", "#2e3648"), ("pressed", "#38425a"), ("disabled", "#2b3140")],
			foreground=[("disabled", "#7f89a3")]
		)
		style.configure("Accent.TButton", background=accent, foreground="white", padding=(14, 7))
		style.map(
			"Accent.TButton",
			background=[("active", accent_hover), ("pressed", accent_hover), ("disabled", "#4d5f92")],
			foreground=[("disabled", "#dbe3fa")]
		)
		style.configure("Treeview", background="#1b202b", fieldbackground="#1b202b", foreground=fg, rowheight=60, borderwidth=0)
		style.configure("Treeview.Heading", background=panel_alt, foreground=fg, font=("Segoe UI Semibold", 10))
		style.map("Treeview", background=[("selected", "#3a4668")], foreground=[("selected", "#ffffff")])
		style.configure("Horizontal.TScrollbar", background=panel_alt)
		style.configure("Vertical.TScrollbar", background=panel_alt)
		style.configure("Status.TLabel", background=panel_alt, foreground=muted, padding=(10, 6))
		style.configure("TPanedwindow", background=bg, sashthickness=6)

	def __init__(self, root):
		self.root = root
		self.root.title("FVP引擎 立绘查看与合成工具")
		self.root.geometry("1600x900")
		self.root.minsize(1280, 760)
		self._configure_styles()
		self.root.update_idletasks()
		# 回到标准窗口模式（性能更稳定）
		self._enable_dark_title_bar()

		# 变量
		self.input_file = None
		self.file_infos = []		  # 所有文件信息
		self.role_dict = {}			# 角色名 -> 文件信息列表
		self.current_preview_images = []  # 当前预览的图像列表（PIL）
		self.current_preview_index = 0	# 当前显示的帧索引
		self.current_part_info = None	 # 当前选择的部件信息
		self.current_part_frame_idx = 0  # 当前选择的部件帧索引
		self.current_composed_image = None  # 当前合成的图像（PIL）
		self.role_images = {}  # 存储角色头像的PhotoImage对象
		self.outfit_images = {}  # 存储服装头像的PhotoImage对象
		self.thumb_buttons = []  # 存储缩略图按钮引用
		self.thumb_images = []   # 存储缩略图PhotoImage防止被垃圾回收

		# 顶部工具栏
		toolbar = ttk.Frame(root, style="Toolbar.TFrame", padding=(12, 8))
		toolbar.pack(fill=tk.X)
		ttk.Label(toolbar, text="FVP Tachie Composer", style="Header.TLabel").pack(side=tk.LEFT)
		ttk.Label(toolbar, text="打开 BIN -> 选择底图 -> 选择差分帧 -> 合成/导出", style="Muted.TLabel").pack(side=tk.LEFT, padx=(12, 0))
		ttk.Button(toolbar, text="打开 BIN", command=self.open_file, style="Accent.TButton").pack(side=tk.RIGHT, padx=(6, 0))
		ttk.Button(toolbar, text="使用说明", command=self.show_help).pack(side=tk.RIGHT)

		# 主布局容器
		body = ttk.Frame(root, padding=(10, 10, 10, 6))
		body.pack(fill=tk.BOTH, expand=True)

		# 主布局：三栏
		main_pane = tk.PanedWindow(
			body,
			orient=tk.HORIZONTAL,
			bg=self.theme_colors["bg"],
			sashwidth=6,
			sashrelief=tk.FLAT,
			bd=0
		)
		main_pane.pack(fill=tk.BOTH, expand=True)

		# 左侧：角色树（带滚动条）
		left_frame = ttk.Frame(main_pane, style="Card.TFrame", width=170, padding=(10, 10))
		main_pane.add(left_frame, minsize=150, stretch="always")
		ttk.Label(left_frame, text="角色列表", style="Header.TLabel").pack(anchor=tk.W)

		# 创建带滚动条的树视图容器
		tree_container = ttk.Frame(left_frame, style="Card.TFrame")
		tree_container.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

		# 垂直滚动条
		tree_scrollbar = ttk.Scrollbar(tree_container)
		tree_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

		# 树视图
		self.tree = ttk.Treeview(tree_container, columns=("type",), show="tree",
								 yscrollcommand=tree_scrollbar.set)
		self.tree.heading("#0", text="名称")
		self.tree.column("#0", width=160)
		self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

		# 关联滚动条
		tree_scrollbar.config(command=self.tree.yview)

		self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)

		# 中间：预览与合成控制区
		mid_frame = ttk.Frame(main_pane, style="Card.TFrame", padding=(10, 10))
		main_pane.add(mid_frame, minsize=420, stretch="always")

		# 底图预览区域
		base_preview_frame = ttk.LabelFrame(mid_frame, text="底图预览", padding=(10, 10))
		base_preview_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
		self.base_preview_frame = base_preview_frame

		self.preview_label = ttk.Label(base_preview_frame, text="请选择一个底图文件")
		self.preview_label.pack(pady=(4, 10))

		self.frame_control = ttk.Frame(base_preview_frame)
		self.frame_control.pack()
		self.prev_btn = ttk.Button(self.frame_control, text="上一帧", command=self.prev_frame, state=tk.DISABLED)
		self.prev_btn.pack(side=tk.LEFT, padx=5)
		self.frame_label = ttk.Label(self.frame_control, text="帧 0/0")
		self.frame_label.pack(side=tk.LEFT, padx=5)
		self.next_btn = ttk.Button(self.frame_control, text="下一帧", command=self.next_frame, state=tk.DISABLED)
		self.next_btn.pack(side=tk.LEFT, padx=5)

		# 部件预览区域
		part_preview_frame = ttk.LabelFrame(mid_frame, text="部件预览", padding=(10, 10))
		part_preview_frame.pack(fill=tk.BOTH, expand=True)
		self.part_preview_frame = part_preview_frame

		# 部件缩略图滚动区域（横向）
		thumb_scroll_frame = ttk.Frame(part_preview_frame)
		thumb_scroll_frame.pack(fill=tk.X, pady=(2, 8))
		
		# 横向滚动条
		thumb_scrollbar = ttk.Scrollbar(thumb_scroll_frame, orient=tk.HORIZONTAL)
		thumb_scrollbar.pack(side=tk.BOTTOM, fill=tk.X)
		
		# 缩略图画布（支持横向滚动）
		self.thumb_canvas = tk.Canvas(thumb_scroll_frame, height=120, bg="#1b202b", highlightthickness=0,
									  xscrollcommand=thumb_scrollbar.set)
		self.thumb_canvas.pack(side=tk.LEFT, fill=tk.X, expand=True)
		thumb_scrollbar.config(command=self.thumb_canvas.xview)
		
		# 缩略图容器
		self.thumb_container = ttk.Frame(self.thumb_canvas)
		self.thumb_canvas.create_window((0, 0), window=self.thumb_container, anchor=tk.NW)
		self.thumb_container.bind("<Configure>", 
			lambda e: self.thumb_canvas.configure(scrollregion=self.thumb_canvas.bbox("all")))
		
		# 鼠标滚轮支持（横向滚动）
		def _on_mousewheel(event):
			self.thumb_canvas.xview_scroll(int(-1*(event.delta/120)), "units")
		self.thumb_canvas.bind("<MouseWheel>", _on_mousewheel)
		
		# 存储缩略图相关数据
		self.thumb_buttons = []  # 存储缩略图按钮引用
		self.thumb_images = []   # 存储缩略图PhotoImage防止GC

		# 操作按钮行
		btn_row = ttk.Frame(part_preview_frame)
		btn_row.pack(fill=tk.X, pady=5)
		self.compose_btn = ttk.Button(btn_row, text="合成预览", command=self.compose_preview, state=tk.DISABLED, style="Accent.TButton")
		self.compose_btn.pack(side=tk.LEFT)

		# 右侧：合成结果预览区
		right_frame = ttk.Frame(main_pane, style="Card.TFrame", padding=(10, 10))
		main_pane.add(right_frame, minsize=420, stretch="always")

		compose_result_frame = ttk.LabelFrame(right_frame, text="合成预览结果", padding=(10, 10))
		compose_result_frame.pack(fill=tk.BOTH, expand=True)
		self.compose_result_frame = compose_result_frame

		self.compose_img_label = ttk.Label(compose_result_frame)
		self.compose_img_label.pack(pady=10)

		# 保存按钮行
		save_row = ttk.Frame(compose_result_frame)
		save_row.pack(fill=tk.X, pady=5)
		save_btn = ttk.Button(save_row, text="保存当前图", command=self.save_composed)
		save_btn.pack(side=tk.LEFT, padx=(0, 6))
		compose_all_btn = ttk.Button(save_row, text="批量合成并导出", command=self.compose_all_diffs, style="Accent.TButton")
		compose_all_btn.pack(side=tk.LEFT)

		# 状态栏
		self.status = ttk.Label(root, text="就绪", anchor=tk.W, style="Status.TLabel")
		self.status.pack(side=tk.BOTTOM, fill=tk.X)
		self.root.after(150, self.open_file)

	def _fit_image_for_widget(self, image, widget, fallback_size=(320, 320), margin=20, fixed_max_size=None):
		"""按控件当前可视尺寸缩放图片，避免超出显示区域"""
		if fixed_max_size is not None:
			max_w, max_h = fixed_max_size
		else:
			widget.update_idletasks()
			max_w = widget.winfo_width() - margin
			max_h = widget.winfo_height() - margin
			if max_w <= 1 or max_h <= 1:
				max_w, max_h = fallback_size
		max_w = max(64, max_w)
		max_h = max(64, max_h)
		resized = image.copy()
		resized.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
		return resized

	def show_help(self):
		"""显示使用说明"""
		help_text = (
			"FVPTachieComposer 使用说明\n\n"
			"【快速开始】\n"
			"1. 程序启动后会自动弹出 BIN 文件选择框。\n"
			"2. 选择 .bin 后，左侧会显示角色 -> 分类 -> 动作层级。\n"
			"3. 点击任意底图，即可在中间看到底图预览。\n\n"
			"【表情与合成】\n"
			"1. 底图被选中后，会自动加载对应“_表情”部件帧。\n"
			"2. 点击任意缩略图，会自动预览该部件并生成合成结果。\n"
			"3. 若需刷新右侧结果，可手动点击“合成预览”。\n\n"
			"【导出】\n"
			"1. 保存当前图：导出当前右侧显示的合成结果。\n"
			"2. 批量合成并导出：按当前底图对应的所有表情帧批量生成 PNG。\n\n"
			"【说明】\n"
			"- 本工具不仅可用于立绘，也可查看包内其他可识别图像资源。\n"
			"- 若某文件解析失败，通常是资源格式异常或数据不完整。"
		)
		messagebox.showinfo("使用说明", help_text)

	def extract_role_avatar(self, role_name, role_infos):
		"""提取角色的第一个底图作为头像"""
		# 找到第一个非表情的底图文件
		base_info = None
		for info in role_infos:
			if info['type'] == 'hzc' and not info['filename'].endswith('_表情'):
				base_info = info
				break
		
		if not base_info:
			return None
		
		try:
			with open(self.input_file, 'rb') as f:
				f.seek(base_info['offset'])
				data = f.read(base_info['size'])
			
			header_info = {
				'image_type': base_info.get('image_type', 0),
				'width': base_info.get('width', 0),
				'height': base_info.get('height', 0),
				'frame_count': base_info.get('frame_count', 1)
			}
			images = hzc_data_to_pil_list(data, header_info)
			if images:
				# 缩放到50x50作为头像（适合Treeview列宽）
				avatar = images[0].copy()
				avatar.thumbnail((50, 50), Image.Resampling.LANCZOS)
				photo = ImageTk.PhotoImage(avatar)
				return photo
		except Exception as e:
			print(f"提取角色 {role_name} 头像失败: {e}")
		return None

	def open_file(self):
		file_path = filedialog.askopenfilename(
			title="选择 BIN 文件",
			filetypes=[("BIN files", "*.bin"), ("All files", "*.*")]
		)
		if not file_path:
			return
		self.input_file = file_path
		self.status.config(text=f"正在解析: {file_path}")
		self.root.update_idletasks()

		try:
			# 解析文件信息
			self.file_infos = parse_bin_info_extended(file_path)
		except Exception as e:
			messagebox.showerror("错误", f"解析失败: {e}")
			self.status.config(text="就绪")
			return

		# 按角色和服装分类（三级结构：角色 -> 服装 -> 动作）
		self.role_dict.clear()
		self.hierarchical_dict = {}  # 新的层级字典: {role: {outfit: [infos]}}
		
		for info in self.file_infos:
			if info['type'] != 'hzc':
				continue
			parts = info['filename'].split('_')
			if len(parts) >= 2 and parts[0] == 'CHR':
				role = parts[1]
				# 解析服装和动作信息
				# 文件名格式示例: CHR_つかさ_夏制服_喜_通常
				# parts[0]=CHR, parts[1]=角色名, parts[2]=服装, parts[3]=表情类型, parts[4]=动作版本
				if len(parts) >= 4:
					outfit = parts[3]  # 服装类型（如：夏制服、夏私服、水着）
				else:
					outfit = "默认"
				self.role_dict.setdefault(role, []).append(info)
				self.hierarchical_dict.setdefault(role, {}).setdefault(outfit, []).append(info)
			else:
				# 非标准命名（不以 CHR_ 开头，如 "ネコ"）：使用文件名本身作为角色名
				role = info['filename']
				outfit = "默认"
				self.role_dict.setdefault(role, []).append(info)
				self.hierarchical_dict.setdefault(role, {}).setdefault(outfit, []).append(info)

		# 更新树（三级层级结构：角色 -> 服装 -> 动作）
		self.tree.delete(*self.tree.get_children())
		self.role_images.clear()  # 清空旧头像
		self.outfit_images.clear()  # 清空旧服装头像
		
		for role, outfits in sorted(self.hierarchical_dict.items()):
			# 第一级：角色名称（带头像）
			all_role_infos = []
			for outfit_infos in outfits.values():
				all_role_infos.extend(outfit_infos)
			
			avatar = self.extract_role_avatar(role, all_role_infos)
			if avatar:
				self.role_images[role] = avatar  # 保存引用防止被GC
				role_node = self.tree.insert("", "end", text=role, image=avatar, open=False)
			else:
				role_node = self.tree.insert("", "end", text=role, open=False)
			
			# 第二级：服装类型
			for outfit, infos in sorted(outfits.items()):
				outfit_avatar = self.extract_role_avatar(f"{role}_{outfit}", infos)
				outfit_key = f"{role}::{outfit}"
				if outfit_avatar:
					self.outfit_images[outfit_key] = outfit_avatar  # 保存引用防止被GC
					outfit_node = self.tree.insert(role_node, "end", text=outfit, image=outfit_avatar, open=False)
				else:
					outfit_node = self.tree.insert(role_node, "end", text=outfit, open=False)
				
				# 第三级：具体动作文件（不包含表情文件）
				for info in sorted(infos, key=lambda x: x['filename']):
					if not info['filename'].endswith('_表情'):
						# 提取动作名称（去掉CHR_角色_表情_服装_前缀）
						parts = info['filename'].split('_')
						if len(parts) >= 5:
							action_name = parts[4]  # 例如: 通常、L等
						else:
							action_name = info['filename']
						
						self.tree.insert(outfit_node, "end", text=action_name, values=(info['type'],), iid=info['filename'])

		self.status.config(text=f"加载完成，共 {len(self.file_infos)} 个文件")
		self.clear_preview()

	def clear_preview(self):
		"""清空所有预览图像"""
		self.preview_label.config(image='', text='请选择一个底图文件')
		self.preview_label.image = None
		self.frame_label.config(text="帧 0/0")
		self.prev_btn.config(state=tk.DISABLED)
		self.next_btn.config(state=tk.DISABLED)
		self.current_preview_images = []
		self.current_preview_index = 0
		self.compose_btn.config(state=tk.DISABLED)
		self.compose_img_label.config(image='')
		self.compose_img_label.image = None
		self.current_composed_image = None
		self.current_part_info = None
		self.current_part_frame_idx = 0
		# 清空缩略图
		self.clear_thumbnails()

	def on_tree_select(self, event):
		selected = self.tree.selection()
		if not selected:
			return
		item = selected[0]
		filename = item
		info = next((i for i in self.file_infos if i['filename'] == filename), None)
		if not info or info['type'] != 'hzc':
			return

		# 如果是表情文件，不预览底图，仅清空预览区并提示
		if filename.endswith('_表情'):
			self.clear_preview()
			self.preview_label.config(text="表情文件请在合成区预览")
			return

		# 否则是底图，正常预览
		try:
			with open(self.input_file, 'rb') as f:
				f.seek(info['offset'])
				data = f.read(info['size'])
		except Exception as e:
			messagebox.showerror("错误", f"读取文件失败: {e}")
			return

		header_info = {
			'image_type': info.get('image_type', 0),
			'width': info.get('width', 0),
			'height': info.get('height', 0),
			'frame_count': info.get('frame_count', 1)
		}
		self.current_preview_images = hzc_data_to_pil_list(data, header_info)
		if not self.current_preview_images:
			messagebox.showerror("错误", "无法解析 HZC 图像")
			return

		self.current_preview_index = 0
		self.show_current_frame()

		if len(self.current_preview_images) > 1:
			self.prev_btn.config(state=tk.NORMAL)
			self.next_btn.config(state=tk.NORMAL)
		else:
			self.prev_btn.config(state=tk.DISABLED)
			self.next_btn.config(state=tk.DISABLED)

		self.update_part_thumbnails(info)

	def show_current_frame(self):
		if not self.current_preview_images:
			return
		img = self.current_preview_images[self.current_preview_index]
		# 底图预览使用固定上限，避免点击切换时容器被反向撑大
		display_img = self._fit_image_for_widget(
			img,
			self.base_preview_frame,
			fallback_size=(430, 430),
			fixed_max_size=(520, 420)
		)
		photo = ImageTk.PhotoImage(display_img)
		self.preview_label.config(image=photo, text='')
		self.preview_label.image = photo
		self.frame_label.config(text=f"帧 {self.current_preview_index+1}/{len(self.current_preview_images)}")

	def prev_frame(self):
		if self.current_preview_index > 0:
			self.current_preview_index -= 1
			self.show_current_frame()

	def next_frame(self):
		if self.current_preview_index < len(self.current_preview_images) - 1:
			self.current_preview_index += 1
			self.show_current_frame()

	def clear_thumbnails(self):
		"""清空缩略图列表"""
		for btn_frame, btn, label in self.thumb_buttons:
			btn_frame.destroy()
		self.thumb_buttons.clear()
		self.thumb_images.clear()
		self.thumb_canvas.xview_moveto(0)

	def update_part_thumbnails(self, base_info):
		"""根据当前选中的底图，更新部件缩略图列表"""
		self.clear_thumbnails()
		
		part_filename = base_info['filename'] + "_表情"
		part_info = next((i for i in self.file_infos if i['filename'] == part_filename and i['type'] == 'hzc'), None)
		if not part_info:
			self.compose_btn.config(state=tk.DISABLED)
			return

		# 读取部件数据生成缩略图
		try:
			with open(self.input_file, 'rb') as f:
				f.seek(part_info['offset'])
				part_data = f.read(part_info['size'])
		except Exception as e:
			print(f"读取部件文件失败: {e}")
			return

		part_header = {
			'image_type': part_info.get('image_type', 0),
			'width': part_info.get('width', 0),
			'height': part_info.get('height', 0),
			'frame_count': part_info.get('frame_count', 1)
		}
		part_imgs = hzc_data_to_pil_list(part_data, part_header)
		if not part_imgs:
			return

		frame_count = len(part_imgs)
		self.current_part_info = part_info
		
		# 创建缩略图按钮（横向排列）
		for idx, part_img in enumerate(part_imgs):
			# 缩放到合适大小（100x100以内）
			thumb = part_img.copy()
			thumb.thumbnail((100, 100), Image.Resampling.LANCZOS)
			photo = ImageTk.PhotoImage(thumb)
			self.thumb_images.append(photo)
			
			# 创建带边框的按钮
			btn_frame = ttk.Frame(self.thumb_container, relief=tk.RIDGE, borderwidth=2)
			btn_frame.pack(side=tk.LEFT, padx=3, pady=3)
			
			btn = ttk.Label(btn_frame, image=photo)
			btn.pack()
			
			# 帧编号标签
			label = ttk.Label(btn_frame, text=f"{idx}", font=("", 8))
			label.pack()
			
			# 绑定点击事件
			btn.bind("<Button-1>", lambda e, i=idx: self.on_thumbnail_click(i))
			btn_frame.bind("<Button-1>", lambda e, i=idx: self.on_thumbnail_click(i))
			label.bind("<Button-1>", lambda e, i=idx: self.on_thumbnail_click(i))
			
			self.thumb_buttons.append((btn_frame, btn, label))
		
		# 默认选中第一帧
		self.on_thumbnail_click(0)
		self.compose_btn.config(state=tk.NORMAL)

	def on_thumbnail_click(self, frame_idx):
		"""点击缩略图时的处理"""
		self.current_part_frame_idx = frame_idx
		
		# 更新选中状态（高亮）
		for idx, (btn_frame, btn, label) in enumerate(self.thumb_buttons):
			if idx == frame_idx:
				btn_frame.config(relief=tk.SUNKEN, borderwidth=3)
				label.config(foreground="blue")
			else:
				btn_frame.config(relief=tk.RIDGE, borderwidth=2)
				label.config(foreground="black")
		
		# 点击缩略图后仅在右侧更新合成结果
		self.compose_preview()

	def preview_part(self):
		"""预览当前选中的部件帧"""
		if not hasattr(self, 'current_part_info') or not self.current_part_info:
			return
		part_info = self.current_part_info
		frame_idx = self.current_part_frame_idx

		try:
			with open(self.input_file, 'rb') as f:
				f.seek(part_info['offset'])
				part_data = f.read(part_info['size'])
		except Exception as e:
			messagebox.showerror("错误", f"读取部件文件失败: {e}")
			return

		part_header = {
			'image_type': part_info.get('image_type', 0),
			'width': part_info.get('width', 0),
			'height': part_info.get('height', 0),
			'frame_count': part_info.get('frame_count', 1)
		}
		part_imgs = hzc_data_to_pil_list(part_data, part_header)
		if not part_imgs or frame_idx >= len(part_imgs):
			messagebox.showerror("错误", "无法解析部件或帧索引无效")
			return
		part_img = part_imgs[frame_idx]

		display_img = self._fit_image_for_widget(part_img, self.part_preview_frame, fallback_size=(360, 260))
		photo = ImageTk.PhotoImage(display_img)
		self.part_preview_label.config(image=photo)
		self.part_preview_label.image = photo

	def compose_preview(self):
		"""执行合成预览，结果显示在右侧"""
		if not hasattr(self, 'current_part_info') or not self.current_part_info:
			return

		selected = self.tree.selection()
		if not selected:
			return
		base_filename = selected[0]
		base_info = next((i for i in self.file_infos if i['filename'] == base_filename), None)
		if not base_info:
			return

		part_info = self.current_part_info
		# 获取当前选中的帧索引（从缩略图）
		frame_idx = self.current_part_frame_idx

		# 读取底图
		try:
			with open(self.input_file, 'rb') as f:
				f.seek(base_info['offset'])
				base_data = f.read(base_info['size'])
		except Exception as e:
			messagebox.showerror("错误", f"读取底图文件失败: {e}")
			return

		base_header = {
			'image_type': base_info.get('image_type', 0),
			'width': base_info.get('width', 0),
			'height': base_info.get('height', 0),
			'frame_count': base_info.get('frame_count', 1)
		}
		base_imgs = hzc_data_to_pil_list(base_data, base_header)
		if not base_imgs:
			messagebox.showerror("错误", "无法解析底图")
			return
		base_img = base_imgs[0]

		# 读取部件
		try:
			with open(self.input_file, 'rb') as f:
				f.seek(part_info['offset'])
				part_data = f.read(part_info['size'])
		except Exception as e:
			messagebox.showerror("错误", f"读取部件文件失败: {e}")
			return

		part_header = {
			'image_type': part_info.get('image_type', 0),
			'width': part_info.get('width', 0),
			'height': part_info.get('height', 0),
			'frame_count': part_info.get('frame_count', 1)
		}
		part_imgs = hzc_data_to_pil_list(part_data, part_header)
		if not part_imgs or frame_idx >= len(part_imgs):
			messagebox.showerror("错误", "无法解析部件或帧索引无效")
			return
		part_img = part_imgs[frame_idx]

		offset_x = part_info.get('offset_x', 0)
		offset_y = part_info.get('offset_y', 0)
		composed = compose_preview(base_img, part_img, offset_x, offset_y)
		self.current_composed_image = composed.copy()  # 保存原图用于保存

		# 缩放显示在右侧
		display_img = self._fit_image_for_widget(composed, self.compose_result_frame, fallback_size=(560, 560))
		photo = ImageTk.PhotoImage(display_img)
		self.compose_img_label.config(image=photo)
		self.compose_img_label.image = photo

	def save_composed(self):
		"""保存当前合成图像到文件，如果没有部件则直接保存底图"""
		selected = self.tree.selection()
		if not selected:
			messagebox.showwarning("警告", "请先选择一个底图文件。")
			return
		base_filename = selected[0]
		base_info = next((i for i in self.file_infos if i['filename'] == base_filename), None)
		if not base_info or base_info['type'] != 'hzc' or base_filename.endswith('_表情'):
			messagebox.showwarning("警告", "请选择一个非表情的底图文件。")
			return

		# 检查是否有对应部件
		part_filename = base_filename + "_表情"
		part_info = next((i for i in self.file_infos if i['filename'] == part_filename and i['type'] == 'hzc'), None)

		if not part_info:
			# 没有部件，直接保存底图
			try:
				with open(self.input_file, 'rb') as f:
					f.seek(base_info['offset'])
					base_data = f.read(base_info['size'])
			except Exception as e:
				messagebox.showerror("错误", f"读取底图文件失败: {e}")
				return

			base_header = {
				'image_type': base_info.get('image_type', 0),
				'width': base_info.get('width', 0),
				'height': base_info.get('height', 0),
				'frame_count': base_info.get('frame_count', 1)
			}
			base_imgs = hzc_data_to_pil_list(base_data, base_header)
			if not base_imgs:
				messagebox.showerror("错误", "无法解析底图")
				return
			base_img = base_imgs[0]  # 取第一帧

			file_path = filedialog.asksaveasfilename(
				defaultextension=".png",
				filetypes=[("PNG files", "*.png"), ("All files", "*.*")],
				title="保存底图",
				initialfile=f"{base_filename}.png"
			)
			if not file_path:
				return
			try:
				base_img.save(file_path, "PNG")
				messagebox.showinfo("成功", f"底图已保存至: {file_path}")
			except Exception as e:
				messagebox.showerror("错误", f"保存失败: {e}")
		else:
			# 有部件，需要合成预览图像（现有逻辑）
			if self.current_composed_image is None:
				messagebox.showwarning("警告", "请先进行合成预览。")
				return
			file_path = filedialog.asksaveasfilename(
				defaultextension=".png",
				filetypes=[("PNG files", "*.png"), ("All files", "*.*")],
				title="保存合成图像"
			)
			if not file_path:
				return
			try:
				self.current_composed_image.save(file_path, "PNG")
				messagebox.showinfo("成功", f"图像已保存至: {file_path}")
			except Exception as e:
				messagebox.showerror("错误", f"保存失败: {e}")

	def compose_all_diffs(self):
		"""合成当前底图对应的所有表情部件帧，并批量保存；若无部件则直接保存底图"""
		selected = self.tree.selection()
		if not selected:
			messagebox.showwarning("警告", "请先选择一个底图文件。")
			return
		base_filename = selected[0]
		base_info = next((i for i in self.file_infos if i['filename'] == base_filename), None)
		if not base_info or base_info['type'] != 'hzc' or base_filename.endswith('_表情'):
			messagebox.showwarning("警告", "请选择一个非表情的底图文件。")
			return

		# 获取对应的部件信息
		part_filename = base_filename + "_表情"
		part_info = next((i for i in self.file_infos if i['filename'] == part_filename and i['type'] == 'hzc'), None)

		# 选择保存目录
		save_dir = filedialog.askdirectory(title="选择保存图像的目录")
		if not save_dir:
			return

		# 读取底图数据
		try:
			with open(self.input_file, 'rb') as f:
				f.seek(base_info['offset'])
				base_data = f.read(base_info['size'])
		except Exception as e:
			messagebox.showerror("错误", f"读取底图文件失败: {e}")
			return

		base_header = {
			'image_type': base_info.get('image_type', 0),
			'width': base_info.get('width', 0),
			'height': base_info.get('height', 0),
			'frame_count': base_info.get('frame_count', 1)
		}
		base_imgs = hzc_data_to_pil_list(base_data, base_header)
		if not base_imgs:
			messagebox.showerror("错误", "无法解析底图")
			return
		base_img = base_imgs[0]  # 取第一帧

		if not part_info:
			# 没有部件，直接保存底图
			out_filename = f"{base_filename}.png"
			out_path = os.path.join(save_dir, out_filename)
			try:
				base_img.save(out_path, "PNG")
				messagebox.showinfo("完成", f"图像已保存到:\n{out_path}")
			except Exception as e:
				messagebox.showerror("错误", f"保存文件失败: {e}")
			return  # 关键：函数结束，避免后续代码

		# 有部件，合成所有部件帧
		try:
			with open(self.input_file, 'rb') as f:
				f.seek(part_info['offset'])
				part_data = f.read(part_info['size'])
		except Exception as e:
			messagebox.showerror("错误", f"读取部件文件失败: {e}")
			return

		part_header = {
			'image_type': part_info.get('image_type', 0),
			'width': part_info.get('width', 0),
			'height': part_info.get('height', 0),
			'frame_count': part_info.get('frame_count', 1)
		}
		part_imgs = hzc_data_to_pil_list(part_data, part_header)
		if not part_imgs:
			messagebox.showerror("错误", "无法解析部件图像")
			return

		offset_x = part_info.get('offset_x', 0)
		offset_y = part_info.get('offset_y', 0)

		saved_count = 0
		for idx, part_img in enumerate(part_imgs):
			composed = compose_preview(base_img, part_img, offset_x, offset_y)
			out_filename = f"{base_filename}_diff_{idx:03d}.png"
			out_path = os.path.join(save_dir, out_filename)
			try:
				composed.save(out_path, "PNG")
				saved_count += 1
			except Exception as e:
				messagebox.showerror("错误", f"保存文件 {out_filename} 失败: {e}")
				break

		messagebox.showinfo("完成", f"成功保存 {saved_count} 个合成图像到目录:\n{save_dir}")

# ---------- 启动 ----------
if __name__ == '__main__':
	root = tk.Tk()
	app = HZCGUI(root)
	root.mainloop()