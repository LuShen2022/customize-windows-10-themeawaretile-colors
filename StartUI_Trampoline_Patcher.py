import struct
import os
import shutil

REQUIRED_SIZES = {
    "GetThemeAwareBackgroundColor": 16,
    "GetThemeAwareForegroundColor": 18,
}
MIN_RUN_SIZE = min(REQUIRED_SIZES.values())

# 特征：GetThemeAwareForegroundColor 入口 8 字节
SIG_FG = bytes.fromhex("8D41FFA9FDFFFFFF")
# 特征：GetThemeAwareBackgroundColor 入口序言（5 字节入口指令 + push rdi + sub rsp,20h）
SIG_BG = bytes.fromhex("48895C2408574883EC20")

# 覆盖长度由原始指令决定，不随版本变
BG_OVERWRITE = 5
FG_OVERWRITE = 8

# 两个入口 RVA 差的合理范围（实测 0x88，留一点余量）
EXPECTED_GAP = 0x88
GAP_TOLERANCE = 0x10


def strip_quotes(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    return s


def read_pe(path):
    with open(path, "rb") as f:
        data = bytearray(f.read())

    if data[:2] != b"MZ":
        raise ValueError("无效的 MZ 文件")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise ValueError("无效的 PE 文件")

    coff = pe_offset + 4
    num_sections = struct.unpack_from("<H", data, coff + 2)[0]
    opt_size = struct.unpack_from("<H", data, coff + 16)[0]
    opt = coff + 20
    magic = struct.unpack_from("<H", data, opt)[0]
    if magic == 0x20B:
        image_base = struct.unpack_from("<Q", data, opt + 24)[0]
    elif magic == 0x10B:
        image_base = struct.unpack_from("<I", data, opt + 28)[0]
    else:
        raise ValueError("未知 PE 格式")

    section_table = opt + opt_size
    sections = []
    for i in range(num_sections):
        off = section_table + i * 40
        name = data[off:off + 8].rstrip(b"\0").decode("ascii", "replace")
        vsize, va, rawsize, rawptr = struct.unpack_from("<IIII", data, off + 8)
        sections.append({"name": name, "va": va, "rawptr": rawptr, "rawsize": rawsize})
    return data, image_base, sections


def find_all(data, sig, start, end):
    out, pos = [], data.find(sig, start, end)
    while pos != -1:
        out.append(pos)
        pos = data.find(sig, pos + 1, end)
    return out


def find_runs(data, start, end, value, min_size):
    results = []
    pos = start
    while pos < end:
        if data[pos] != value:
            pos += 1
            continue
        run_start = pos
        while pos < end and data[pos] == value:
            pos += 1
        if pos - run_start >= min_size:
            results.append((run_start, pos - run_start))
    return results


def rva_to_offset(rva, text):
    return text["rawptr"] + (rva - text["va"])


def offset_to_rva(off, text):
    return text["va"] + (off - text["rawptr"])


def make_jmp(src_ea, target_ea):
    rel = target_ea - (src_ea + 5)
    return b"\xE9" + struct.pack("<i", rel)


def locate_functions(data, text):
    """特征定位两个函数入口，返回 (bg_off, fg_off) 文件偏移"""
    start = text["rawptr"]
    end = start + text["rawsize"]

    fg_hits = find_all(data, SIG_FG, start, end)
    if len(fg_hits) != 1:
        raise RuntimeError(f"GetThemeAwareForegroundColor 特征命中 {len(fg_hits)} 个，应为 1 个，无法唯一定位")
    fg_off = fg_hits[0]

    bg_hits = [h for h in find_all(data, SIG_BG, start, end) if h < fg_off]
    if not bg_hits:
        raise RuntimeError("GetThemeAwareForegroundColor 之前未找到 GetThemeAwareBackgroundColor 序言")
    bg_off = bg_hits[-1]

    bg_rva = offset_to_rva(bg_off, text)
    fg_rva = offset_to_rva(fg_off, text)
    gap = fg_rva - bg_rva
    if abs(gap - EXPECTED_GAP) > GAP_TOLERANCE:
        raise RuntimeError(
            f"两个入口 RVA 差为 0x{gap:X}，偏离预期 0x{EXPECTED_GAP:X} 过多，拒绝修改"
        )

    return bg_off, fg_off, bg_rva, fg_rva


def main():
    raw = input("请输入 StartUI.dll 路径: ")
    dll_path = strip_quotes(raw)
    if not os.path.isfile(dll_path):
        print("找不到文件:", dll_path)
        return

    print("\n请选择强制设定的 TileThemeSelector 值：")
    print("  1 = 浅色主题下绘制纯黑磁贴，深色主题下绘制纯黑磁贴")
    print("  2 = 浅色主题下绘制纯白磁贴，深色主题下绘制纯白磁贴")
    print("  3 = 浅色主题下绘制深色透明磁贴，深色主题下绘制深色透明磁贴")
    print("  4 = 浅色主题下绘制浅色透明磁贴，深色主题下绘制浅色透明磁贴")
    print("  推荐：如果你使用浅色主题，选 4；如果你使用深色主题，选 3")

    choice = input("请输入 1、2、3 或 4: ").strip()
    if choice not in ("1", "2", "3", "4"):
        print("[-] 只能输入 1、2、3 或 4，退出。")
        return
    target_value = int(choice)
    print(f"[+] 将强制设定为: {target_value}")

    data, image_base, sections = read_pe(dll_path)
    text = next((s for s in sections if s["name"] == ".text"), None)
    if text is None:
        print("没有找到 .text 段")
        return

    start = text["rawptr"]
    end = start + text["rawsize"]

    print("=" * 65)
    print("StartUI.dll 特征定位 + trampoline 补丁")
    print("=" * 65)
    print(f"ImageBase : 0x{image_base:X}")
    print(f".text RVA : 0x{text['va']:X}")
    print(f".text 文件范围: 0x{start:X} - 0x{end:X}")
    print()

    try:
        bg_entry_off, fg_entry_off, bg_entry_rva, fg_entry_rva = locate_functions(data, text)
    except RuntimeError as e:
        print(f"[-] 定位失败: {e}")
        return

    print(f"[+] GetThemeAwareBackgroundColor 入口: 文件 0x{bg_entry_off:X}  RVA 0x{bg_entry_rva:X}")
    print(f"[+] GetThemeAwareForegroundColor 入口: 文件 0x{fg_entry_off:X}  RVA 0x{fg_entry_rva:X}")
    print(f"    两者 RVA 差: 0x{fg_entry_rva - bg_entry_rva:X}")

    bg_entry_ea = image_base + bg_entry_rva
    fg_entry_ea = image_base + fg_entry_rva

    # 校验入口原始字节（特征定位后仍再确认一次覆盖长度内的字节）
    bg_orig = bytes(data[bg_entry_off:bg_entry_off + BG_OVERWRITE])
    fg_orig = bytes(data[fg_entry_off:fg_entry_off + FG_OVERWRITE])
    if bg_orig != bytes.fromhex("48895C2408"):
        print(f"[-] GetThemeAwareBackgroundColor 入口 5 字节不符: {bg_orig.hex(' ').upper()}")
        return
    if fg_orig != SIG_FG:
        print(f"[-] GetThemeAwareForegroundColor 入口 8 字节不符: {fg_orig.hex(' ').upper()}")
        return

    # 扫描空闲区
    candidates = []
    for value, name in [(0xCC, "CC"), (0x00, "00")]:
        for off, size in find_runs(data, start, end, value, MIN_RUN_SIZE):
            candidates.append({"type": name, "off": off, "size": size})

    print(f"\n候选空闲区共 {len(candidates)} 个（>= {MIN_RUN_SIZE} 字节）")

    bg_slot = None
    fg_slot = None
    used = []

    for need, key in [(REQUIRED_SIZES["GetThemeAwareBackgroundColor"], "bg"), (REQUIRED_SIZES["GetThemeAwareForegroundColor"], "fg")]:
        for c in candidates:
            c_start, c_end = c["off"], c["off"] + c["size"]
            if any(not (c_end <= u_start or c_start >= u_end) for u_start, u_end in used):
                continue
            if c["size"] < need:
                continue
            slot = {"off": c["off"], "size": c["size"], "need": need, "type": c["type"]}
            used.append((c["off"], c["off"] + need))
            if key == "bg":
                bg_slot = slot
            else:
                fg_slot = slot
            break

    if bg_slot is None or fg_slot is None:
        print("[-] 无法为两个 trampoline 分配空闲区")
        return

    bg_tramp_rva = offset_to_rva(bg_slot["off"], text)
    fg_tramp_rva = offset_to_rva(fg_slot["off"], text)
    bg_tramp_ea = image_base + bg_tramp_rva
    fg_tramp_ea = image_base + fg_tramp_rva

    # 构造 trampoline
    bg_tramp = b"\x41\xB8" + struct.pack("<I", target_value) + bg_orig
    bg_tramp += make_jmp(bg_tramp_ea + len(bg_tramp), bg_entry_ea + BG_OVERWRITE)

    fg_tramp = b"\xB9" + struct.pack("<I", target_value) + fg_orig
    fg_tramp += make_jmp(fg_tramp_ea + len(fg_tramp), fg_entry_ea + FG_OVERWRITE)

    if len(bg_tramp) > bg_slot["need"] or len(fg_tramp) > fg_slot["need"]:
        print("[-] trampoline 超出空闲区大小")
        return

    bg_entry_patch = make_jmp(bg_entry_ea, bg_tramp_ea)
    fg_entry_patch = make_jmp(fg_entry_ea, fg_tramp_ea) + b"\x90" * 3

    bak = dll_path + ".bak"
    if not os.path.exists(bak):
        shutil.copy2(dll_path, bak)
        print(f"\n[+] 已备份: {bak}")

    data[bg_slot["off"]:bg_slot["off"] + len(bg_tramp)] = bg_tramp
    data[fg_slot["off"]:fg_slot["off"] + len(fg_tramp)] = fg_tramp
    data[bg_entry_off:bg_entry_off + BG_OVERWRITE] = bg_entry_patch
    data[fg_entry_off:fg_entry_off + FG_OVERWRITE] = fg_entry_patch

    with open(dll_path, "wb") as f:
        f.write(data)

    print("\n" + "=" * 65)
    print("补丁完成")
    print("=" * 65)
    print(f"\n[设定值] {target_value}")
    print(f"\n[跳转目标]")
    print(f"  GetThemeAwareBackgroundColor: 0x{bg_entry_ea:X} -> 0x{bg_tramp_ea:X}")
    print(f"  GetThemeAwareForegroundColor: 0x{fg_entry_ea:X} -> 0x{fg_tramp_ea:X}")
    print(f"\n[入口补丁字节]")
    print(f"  GetThemeAwareBackgroundColor: {bg_entry_patch.hex(' ').upper()}")
    print(f"  GetThemeAwareForegroundColor: {fg_entry_patch.hex(' ').upper()}")
    print(f"\n[trampoline 字节]")
    print(f"  GetThemeAwareBackgroundColor: {bg_tramp.hex(' ').upper()}")
    print(f"  GetThemeAwareForegroundColor: {fg_tramp.hex(' ').upper()}")
    print("\n完成。")


if __name__ == "__main__":
    main()