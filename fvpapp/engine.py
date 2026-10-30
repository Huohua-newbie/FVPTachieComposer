"""HZC / BIN 解析与差分合成——界面之下唯一的内核。

原 `FVPTachieComposer.py`（613 行）里活着的那一半，逐行搬来，函数体一字未改：
从 `.bin` 索引里找出文件与偏移、从 HZC 字节里解出帧、把底图与差分按偏移合成。

原来同一个文件里还有一整套**命令行批处理**（`main` / `convert_hzc_data` /
`compose_differentials` / `interactive_filter_by_parts` 等 312 行），GUI 上线后
再没人调用，已随搬迁一并删除。

依赖：只用 pathlib / struct / zlib / Pillow，不碰 flet —— 谁都能单独 import 跑测试。
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Callable
from pathlib import Path

from PIL import Image


def transform_bytes_bytearray(data: bytes) -> bytes:
	"""
	字节变换函数（仅用于多帧 HZC）：每4个字节一组，交换位置0和2
	使用扩展切片在 C 层完成交换，避免逐字节 Python 循环。
	"""
	byte_arr = bytearray(data)
	n4 = (len(byte_arr) // 4) * 4
	byte_arr[0:n4:4], byte_arr[2:n4:4] = data[2:n4:4], data[0:n4:4]
	return bytes(byte_arr)

def hzc_decode_first_frame(hzc_data: bytes, header_info: dict) -> Image.Image | None:
	"""
	仅解码多帧 HZC 的第一帧（供缩略图等场景）：
	zlib 增量解压到首帧所需字节即停，跳过其余帧的解压与 PIL 构建。
	返回 PIL Image 或 None。
	"""
	if len(hzc_data) < 44:
		return None
	width = header_info.get('width') or 0
	height = header_info.get('height') or 0
	image_type = header_info.get('image_type')
	if not width or not height:
		return None
	bpp = 3 if image_type == 0 else 4
	bpf = width * height * bpp
	d = zlib.decompressobj()
	try:
		head = d.decompress(hzc_data[44:], bpf)
		while len(head) < bpf and d.unconsumed_tail:
			chunk = d.decompress(d.unconsumed_tail, bpf - len(head))
			if not chunk:
				break
			head += chunk
	except zlib.error:
		return None
	if len(head) < bpf:
		return None
	if image_type == 2:
		frame = transform_bytes_bytearray(head[:bpf])
		return Image.frombytes('RGBA', (width, height), frame)
	if image_type == 0:
		img = Image.frombytes('RGB', (width, height), head[:bpf])
		b, g, r = img.split()
		return Image.merge("RGB", (r, g, b))
	img = Image.frombytes('RGBA', (width, height), head[:bpf])
	b, g, r, a = img.split()
	return Image.merge("RGBA", (r, g, b, a))

def parse_bin_info(input_file: str, progress: Callable[[int, int, str], None] | None = None) -> list[dict]:
	"""
	解析.bin文件，返回文件信息列表。
	每个元素为字典：{'filename': 文件名（不含扩展名）, 'offset': 绝对偏移, 'size': 大小, 'type': 类型}

	`progress(done, total, stage)` 可选：`total` 恒为条目数，`stage` 是给人看的阶段名。
	前两个阶段（读文件表 / 读文件名）各只是一次顺序读，所以报 0/x 换个阶段名就过去，
	真正逐条推进的是「识别类型」。不传 progress 时行为与原来完全一致。
	"""
	with open(input_file, 'rb') as f:
		header = f.read(8)
		if len(header) != 8:
			raise ValueError("文件头不完整")
		x, y = struct.unpack('<II', header)  # 文件数，文件名总长度

		def report(done, stage):
			if progress is not None:
				progress(min(done, x), x, stage)

		report(0, "读文件表")
		entries = []  # (rel_offset, abs_offset, size)
		for _ in range(x):
			entry_data = f.read(12)
			if len(entry_data) != 12:
				raise ValueError("文件信息表不完整")
			rel_offset, abs_offset, size = struct.unpack('<III', entry_data)
			entries.append((rel_offset, abs_offset, size))

		report(0, "读文件名")
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
			report(idx + 1, "识别类型")

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

def parse_hzc_header_from_bytes(header_bytes: bytes) -> dict | None:
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

def parse_bin_info_extended(input_file: str, progress: Callable[[int, int, str], None] | None = None) -> list[dict]:
	"""调用原 parse_bin_info 并补充 HZC 头部信息

	进度按条目数折成两段：`parse_bin_info` 里的「识别类型」扫一遍条目，
	这里补头部再扫一遍。内层报来的 total 乘 2、done 原样转出去，
	前半段正好走满进度条的一半。
	"""
	if progress is None:
		base_infos = parse_bin_info(input_file)
	else:
		def _inner(done, total, stage):
			progress(done, total * 2, stage)
		base_infos = parse_bin_info(input_file, progress=_inner)
	n = len(base_infos)
	with open(input_file, 'rb') as f:
		for i, info in enumerate(base_infos):
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
			if progress is not None:
				progress(n + i + 1, n * 2, "读图像头")
	return base_infos

def parse_dir_infos(folder: str | Path, progress: Callable[[int, int, str], None] | None = None) -> list[dict]:
	"""
	递归扫描文件夹，识别 HZC 图像文件（按 .hzc 后缀或 hzc1 文件头），
	返回与 parse_bin_info_extended 兼容的信息列表。
	每个元素额外含 'path'（文件绝对路径），offset 恒为 0，size 为文件大小。

	进度分两段：「扫描目录」边数边报，此刻还不知道总共多少个，所以 total 报 0
	表示不确定（界面据此走不确定态）；「读文件头」才有明确的 done/total。
	"""
	folder = Path(folder)
	paths = []
	for p in sorted(folder.rglob('*')):
		if not p.is_file():
			continue
		paths.append(p)
		if progress is not None and len(paths) % 200 == 0:
			progress(len(paths), 0, "扫描目录")
	total = len(paths)
	infos = []
	if progress is not None:
		progress(0, total, "读文件头")
	for i, p in enumerate(paths):
		with open(p, 'rb') as f:
			header_data = f.read(44)
		has_magic = len(header_data) >= 4 and header_data[:4] == b'hzc1'
		if progress is not None:
			progress(i + 1, total, "读文件头")
		if not has_magic and p.suffix.lower() != '.hzc':
			continue
		header = parse_hzc_header_from_bytes(header_data) if has_magic else None
		info = {
			'filename': p.stem,
			'offset': 0,
			'size': p.stat().st_size,
			'type': 'hzc',
			'path': str(p),
		}
		if header:
			info.update(header)
		else:
			info.update({'image_type': None, 'width': 0, 'height': 0, 'offset_x': 0, 'offset_y': 0, 'frame_count': 1})
		infos.append(info)
	return infos

def hzc_data_to_pil_list(hzc_data: bytes, header_info: dict) -> list[Image.Image]:
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

def compose_preview(base_img: Image.Image, part_img: Image.Image, offset_x: int, offset_y: int) -> Image.Image:
	"""将部件图像合成到底图上，返回合成后的 PIL Image"""
	base = base_img.convert('RGBA')
	part = part_img.convert('RGBA')
	# If part size matches base size, treat as full replacement
	if part.size == base.size:
		return part
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
