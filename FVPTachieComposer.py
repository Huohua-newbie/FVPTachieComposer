from __future__ import annotations

from pathlib import Path
import struct
import zlib
from PIL import Image
import os
import ctypes
import logging

log = logging.getLogger("fvptachie.core")

# 说明：本文件是解析/合成核心 + 命令行批处理入口（main），不含 GUI。
# 现役界面是 FVPTachieComposerFlet.py（Flet）。



# ---------- HZC 文件解析与转换 ----------
def parse_hzc_header(header_bytes: bytes) -> dict:
	"""
	解析 HZC 文件头（44 字节）
	返回字典包含关键信息
	"""
	if len(header_bytes) < 44:
		raise ValueError("文件头不足 44 字节")

	magic = header_bytes[0:4].decode('ascii', errors='ignore')
	if magic != "hzc1":
		log.warning("魔数不是 'hzc1'，实际为 %s", magic)

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
	bpf = width * height * 4
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
		need = width * height * 3
		img = Image.frombytes('RGB', (width, height), head[:need])
		r, g, b = img.split()
		return Image.merge("RGB", (r, g, b))
	img = Image.frombytes('RGBA', (width, height), head[:bpf])
	b, g, r, a = img.split()
	return Image.merge("RGBA", (r, g, b, a))

def convert_hzc_data(hzc_data: bytes, original_filename: str, base_output_dir: str | Path) -> dict | None:
	"""
	将 HZC 二进制数据转换为 PNG 图片
	:param hzc_data: 完整的 HZC 文件数据
	:param original_filename: 原始文件名（不含扩展名）
	:param base_output_dir: 基础输出目录（例如 bin 文件名去掉后缀的目录）
	:return: 字典包含处理信息
	"""
	if len(hzc_data) < 44:
		log.error("文件数据过小，不是有效的 HZC 文件")
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
		log.error("解压失败 %s: %s", original_filename, e)
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
				log.warning("帧 %s 数据不足，跳过", i)
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
				log.warning("数据大小不匹配，预期 %s，实际 %s", expected, len(decompressed))
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
def parse_bin_info(input_file: str) -> list[dict]:
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
def interactive_filter_by_parts(file_infos: list[dict]) -> list[dict]:
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
def extract_and_convert_by_condition(input_file: str, file_infos: list[dict], output_dir: str | Path, condition_func) -> list[dict]:
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
def compose_differentials(base_output_dir: str | Path, converted_infos: list[dict]) -> None:
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
			log.warning("底图文件不存在 %s", base_img_path)
			continue

		# 对应的表情部件文件夹（与底图同父目录，名为 base_filename + '_表情'）
		emotion_folder = base_dir.parent / (base_filename + "_表情")
		if str(emotion_folder) not in emotion_map:
			continue

		emotion_info = emotion_map[str(emotion_folder)]
		offset_x = emotion_info['offset_x']
		offset_y = emotion_info['offset_y']
		if offset_x is None or offset_y is None:
			log.warning("部件文件夹 %s 缺少偏移信息，跳过", emotion_folder)
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

def parse_bin_info_extended(input_file: str) -> list[dict]:
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

# ---------- 解析散装 HZC 文件夹 ----------
def parse_dir_infos(folder: str | Path) -> list[dict]:
	"""
	递归扫描文件夹，识别 HZC 图像文件（按 .hzc 后缀或 hzc1 文件头），
	返回与 parse_bin_info_extended 兼容的信息列表。
	每个元素额外含 'path'（文件绝对路径），offset 恒为 0，size 为文件大小。
	"""
	folder = Path(folder)
	infos = []
	for p in sorted(folder.rglob('*')):
		if not p.is_file():
			continue
		with open(p, 'rb') as f:
			header_data = f.read(44)
		has_magic = len(header_data) >= 4 and header_data[:4] == b'hzc1'
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

# ---------- 将 HZC 数据转换为 PIL 图像列表 ----------
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

# ---------- 合成差分图像 ----------
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

# ---------- 命令行入口 ----------
def main(argv: list[str] | None = None) -> int:
	"""命令行批处理：把一个 .bin 里的贴图导出成 PNG，并合成表情差分。

	    py -3.13 FVPTachieComposer.py <资源.bin> [-o 输出目录] [--all]

	不带 --all 时按文件名逐级交互筛选（与早期 CLI 行为一致）。
	图形界面（HZCGUI）已移除，本文件现在只作为解析/合成核心 + 这个批处理入口。
	"""
	import argparse

	parser = argparse.ArgumentParser(
		prog="FVPTachieComposer",
		description="从 FVP .bin 中导出贴图 PNG，并合成表情差分。",
	)
	parser.add_argument("input", help="要处理的 .bin 文件")
	parser.add_argument(
		"-o", "--output",
		help="输出根目录（默认：bin 文件名去掉后缀，建在同级目录）",
	)
	parser.add_argument(
		"--all", action="store_true",
		help="跳过交互筛选，处理全部文件",
	)
	args = parser.parse_args(argv)

	# 库本身不配 handler；CLI 场景把 WARNING 及以上打到 stderr，别让警告默默消失
	if not logging.getLogger().handlers:
		logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

	input_path = Path(args.input)
	if not input_path.is_file():
		print(f"错误: 找不到文件 {input_path}")
		return 1

	output_dir = Path(args.output) if args.output else input_path.with_suffix("")
	output_dir.mkdir(parents=True, exist_ok=True)

	file_infos = parse_bin_info(str(input_path))
	if not file_infos:
		print("错误: 未在 bin 中解析到任何文件")
		return 1

	selected = file_infos if args.all else interactive_filter_by_parts(file_infos)
	print(f"\n开始导出 {len(selected)} 个文件到 {output_dir} ...")
	converted = extract_and_convert_by_condition(
		str(input_path), selected, output_dir, lambda info: True
	)
	compose_differentials(output_dir, converted)
	print("完成。")
	return 0


if __name__ == '__main__':
	raise SystemExit(main())
