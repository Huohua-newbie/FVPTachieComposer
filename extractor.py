import struct
import zlib
from pathlib import Path
from PIL import Image

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

# ---------- 主程序 ----------
if __name__ == '__main__':
    input_file = input("输入待解包文件名：").strip()
    output_dir = input_file.rsplit('.', 1)[0]  # 以 bin 文件名（不含后缀）作为基础输出目录

    # 1. 解析文件信息
    print("正在解析文件信息...")
    file_infos = parse_bin_info(input_file)

    # 2. 从 HZC 文件中提取角色名（假设命名规则：CHR_角色名_...）
    char_names = set()
    for info in file_infos:
        if info['type'] == 'hzc':
            parts = info['filename'].split('_')
            if len(parts) >= 2 and parts[0] == 'CHR':
                char_names.add(parts[1])  # 角色名是第二个部分
    char_list = sorted(char_names)

    if not char_list:
        print("未找到任何符合命名规则（CHR_角色名_...）的 HZC 文件。")
        exit()

    # 3. 显示角色名并让用户选择
    print("\n检测到以下角色：")
    for i, name in enumerate(char_list, 1):
        print(f"{i}. {name}")

    while True:
        choice = input("\n请输入要提取的角色序号（数字）：").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(char_list):
                selected_char = char_list[idx]
                break
            else:
                print(f"序号范围应为 1~{len(char_list)}，请重新输入。")
        except ValueError:
            print("输入无效，请输入数字。")

    # 获取该角色的所有 HZC 文件
    selected_files = [
        info for info in file_infos
        if info['type'] == 'hzc' and
        len(info['filename'].split('_')) >= 2 and
        info['filename'].split('_')[0] == 'CHR' and
        info['filename'].split('_')[1] == selected_char
    ]

    # 4. 可选：按文件名层级进一步细分
    print("\n是否要对文件进行细分选择？")
    print("1. 是，按文件名层级进一步选择")
    print("2. 否，处理该角色的所有文件")
    sub_choice = input("请选择 (1/2): ").strip()
    if sub_choice == '1':
        selected_files = interactive_filter_by_parts(selected_files)
        print(f"细分后共有 {len(selected_files)} 个文件。")
    else:
        print("将处理该角色的所有文件。")

    if not selected_files:
        print("没有选中任何文件，退出。")
        exit()

    # 5. 提取并转换文件
    print(f"\n开始处理角色 '{selected_char}' 的 {len(selected_files)} 个文件...")
    selected_filenames = {info['filename'] for info in selected_files}
    def condition(info):
        return info['filename'] in selected_filenames

    converted = extract_and_convert_by_condition(input_file, file_infos, output_dir, condition)
    print(f"转换完成，共处理 {len(converted)} 个 HZC 文件。")

    # 6. 差分合成
    if converted:
        compose_differentials(output_dir, converted)
    else:
        print("没有转换任何文件，跳过合成。")