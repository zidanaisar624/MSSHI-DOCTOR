use std::{env, fs, path::PathBuf};

fn push_u16(out: &mut Vec<u8>, v: u16) {
    out.extend_from_slice(&v.to_le_bytes());
}

fn push_u32(out: &mut Vec<u8>, v: u32) {
    out.extend_from_slice(&v.to_le_bytes());
}

fn pad_align(out: &mut Vec<u8>, align: usize) {
    while out.len() % align != 0 {
        out.push(0);
    }
}

struct ParsedIco<'a> {
    frames: Vec<(&'a [u8], &'a [u8])>, // (12-byte dir header, raw image payload)
    group_data: Vec<u8>,
}

fn parse_ico(ico: &[u8]) -> Result<ParsedIco<'_>, String> {
    if ico.len() < 6 {
        return Err("ICO file too small".into());
    }
    let reserved = u16::from_le_bytes([ico[0], ico[1]]);
    let ico_type = u16::from_le_bytes([ico[2], ico[3]]);
    let count = u16::from_le_bytes([ico[4], ico[5]]) as usize;
    if reserved != 0 || ico_type != 1 || count == 0 {
        return Err("Invalid ICO header".into());
    }
    let dir_end = 6usize
        .checked_add(count.checked_mul(16).ok_or("ICO directory overflow")?)
        .ok_or("ICO directory overflow")?;
    if ico.len() < dir_end {
        return Err("Truncated ICO directory".into());
    }

    let mut frames = Vec::with_capacity(count);
    let mut group_data = Vec::with_capacity(6 + count * 14);
    push_u16(&mut group_data, 0);
    push_u16(&mut group_data, 1);
    push_u16(&mut group_data, count as u16);

    for idx in 0..count {
        let entry_off = 6 + idx * 16;
        let entry = &ico[entry_off..entry_off + 16];
        let bytes_in_res = u32::from_le_bytes([entry[8], entry[9], entry[10], entry[11]]) as usize;
        let img_offset = u32::from_le_bytes([entry[12], entry[13], entry[14], entry[15]]) as usize;
        let img_end = img_offset
            .checked_add(bytes_in_res)
            .ok_or("ICO image slice overflow")?;
        if img_end > ico.len() {
            return Err(format!("Truncated ICO frame {idx}"));
        }
        let icon_id = (idx + 1) as u16;
        frames.push((&entry[..12], &ico[img_offset..img_end]));
        group_data.extend_from_slice(&entry[..12]);
        push_u16(&mut group_data, icon_id);
    }

    Ok(ParsedIco { frames, group_data })
}

fn append_res_record(out: &mut Vec<u8>, res_type: u16, res_id: u16, memory_flags: u16, data: &[u8]) {
    push_u32(out, data.len() as u32);
    push_u32(out, 32);
    push_u16(out, 0xFFFF);
    push_u16(out, res_type);
    push_u16(out, 0xFFFF);
    push_u16(out, res_id);
    push_u32(out, 0);
    push_u16(out, memory_flags);
    push_u16(out, 0x0409);
    push_u32(out, 0);
    push_u32(out, 0);
    out.extend_from_slice(data);
    pad_align(out, 4);
}

fn build_msvc_res(parsed: &ParsedIco<'_>) -> Vec<u8> {
    let mut res = Vec::new();
    push_u32(&mut res, 0);
    push_u32(&mut res, 32);
    push_u16(&mut res, 0xFFFF);
    push_u16(&mut res, 0);
    push_u16(&mut res, 0xFFFF);
    push_u16(&mut res, 0);
    res.extend_from_slice(&[0u8; 16]);

    for (idx, (_, payload)) in parsed.frames.iter().enumerate() {
        append_res_record(&mut res, 3, (idx + 1) as u16, 0x1010, payload);
    }
    append_res_record(&mut res, 14, 1, 0x1030, &parsed.group_data);
    res
}

fn push_res_dir_header(out: &mut Vec<u8>, id_entries: u16) {
    push_u32(out, 0);
    push_u32(out, 0);
    push_u16(out, 0);
    push_u16(out, 0);
    push_u16(out, 0);
    push_u16(out, id_entries);
}

fn build_gnu_coff_obj(parsed: &ParsedIco<'_>, arch: &str) -> Vec<u8> {
    let (machine, reloc_type): (u16, u16) = match arch {
        "x86" => (0x014C, 0x0007),
        "aarch64" => (0xAA64, 0x0002),
        _ => (0x8664, 0x0003),
    };

    let count = parsed.frames.len();
    let total_items = count + 1;

    let mut rsrc02 = Vec::new();
    let mut data_offsets = Vec::with_capacity(total_items);
    let mut data_sizes = Vec::with_capacity(total_items);
    for (_, payload) in &parsed.frames {
        pad_align(&mut rsrc02, 8);
        data_offsets.push(rsrc02.len() as u32);
        data_sizes.push(payload.len() as u32);
        rsrc02.extend_from_slice(payload);
    }
    pad_align(&mut rsrc02, 8);
    data_offsets.push(rsrc02.len() as u32);
    data_sizes.push(parsed.group_data.len() as u32);
    rsrc02.extend_from_slice(&parsed.group_data);
    pad_align(&mut rsrc02, 8);

    let l1_off = 0u32;
    let l2_icon_off = l1_off + 16 + 2 * 8;
    let l2_group_off = l2_icon_off + 16 + (count as u32) * 8;
    let l3_base_off = l2_group_off + 16 + 8;
    let data_entries_base_off = l3_base_off + (total_items as u32) * 24;

    let mut rsrc01 = Vec::new();
    push_res_dir_header(&mut rsrc01, 2);
    push_u32(&mut rsrc01, 3);
    push_u32(&mut rsrc01, 0x8000_0000 | l2_icon_off);
    push_u32(&mut rsrc01, 14);
    push_u32(&mut rsrc01, 0x8000_0000 | l2_group_off);

    push_res_dir_header(&mut rsrc01, count as u16);
    for i in 0..count {
        push_u32(&mut rsrc01, (i + 1) as u32);
        push_u32(&mut rsrc01, 0x8000_0000 | (l3_base_off + (i as u32) * 24));
    }

    push_res_dir_header(&mut rsrc01, 1);
    push_u32(&mut rsrc01, 1);
    push_u32(&mut rsrc01, 0x8000_0000 | (l3_base_off + (count as u32) * 24));

    for k in 0..total_items {
        push_res_dir_header(&mut rsrc01, 1);
        push_u32(&mut rsrc01, 0x0409);
        push_u32(&mut rsrc01, data_entries_base_off + (k as u32) * 16);
    }

    let mut relocs = Vec::with_capacity(total_items * 10);
    for k in 0..total_items {
        let rva_field_off = rsrc01.len() as u32;
        push_u32(&mut rsrc01, data_offsets[k]);
        push_u32(&mut rsrc01, data_sizes[k]);
        push_u32(&mut rsrc01, 0);
        push_u32(&mut rsrc01, 0);

        push_u32(&mut relocs, rva_field_off);
        push_u32(&mut relocs, 1);
        push_u16(&mut relocs, reloc_type);
    }
    pad_align(&mut rsrc01, 8);

    let hdr_size = 20 + 2 * 40;
    let rsrc01_ptr = hdr_size as u32;
    let rsrc02_ptr = rsrc01_ptr + (rsrc01.len() as u32);
    let reloc_ptr = rsrc02_ptr + (rsrc02.len() as u32);
    let symtab_ptr = reloc_ptr + (relocs.len() as u32);

    let mut obj = Vec::with_capacity((symtab_ptr as usize) + 40);
    push_u16(&mut obj, machine);
    push_u16(&mut obj, 2);
    push_u32(&mut obj, 0);
    push_u32(&mut obj, symtab_ptr);
    push_u32(&mut obj, 2);
    push_u16(&mut obj, 0);
    push_u16(&mut obj, 0);

    let sec_chars = 0x4040_0040u32;
    obj.extend_from_slice(b".rsrc$01");
    push_u32(&mut obj, 0);
    push_u32(&mut obj, 0);
    push_u32(&mut obj, rsrc01.len() as u32);
    push_u32(&mut obj, rsrc01_ptr);
    push_u32(&mut obj, reloc_ptr);
    push_u32(&mut obj, 0);
    push_u16(&mut obj, total_items as u16);
    push_u16(&mut obj, 0);
    push_u32(&mut obj, sec_chars);

    obj.extend_from_slice(b".rsrc$02");
    push_u32(&mut obj, 0);
    push_u32(&mut obj, 0);
    push_u32(&mut obj, rsrc02.len() as u32);
    push_u32(&mut obj, rsrc02_ptr);
    push_u32(&mut obj, 0);
    push_u32(&mut obj, 0);
    push_u16(&mut obj, 0);
    push_u16(&mut obj, 0);
    push_u32(&mut obj, sec_chars);

    obj.extend_from_slice(&rsrc01);
    obj.extend_from_slice(&rsrc02);
    obj.extend_from_slice(&relocs);

    obj.extend_from_slice(b".rsrc$01");
    push_u32(&mut obj, 0);
    push_u16(&mut obj, 1);
    push_u16(&mut obj, 0);
    obj.push(3);
    obj.push(0);

    obj.extend_from_slice(b".rsrc$02");
    push_u32(&mut obj, 0);
    push_u16(&mut obj, 2);
    push_u16(&mut obj, 0);
    obj.push(3);
    obj.push(0);

    push_u32(&mut obj, 4);
    obj
}

fn main() {
    let manifest_dir = PathBuf::from(env::var("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR"));
    let ico_path = manifest_dir.join("../../assets/msshi-doctor.ico");
    println!("cargo:rerun-if-changed={}", ico_path.display());

    if env::var("CARGO_CFG_TARGET_OS").unwrap_or_default() != "windows" {
        return;
    }

    let ico_bytes = fs::read(&ico_path)
        .unwrap_or_else(|e| panic!("Failed to read {}: {e}", ico_path.display()));
    let parsed = parse_ico(&ico_bytes)
        .unwrap_or_else(|e| panic!("Failed to parse {}: {e}", ico_path.display()));

    let out_dir = PathBuf::from(env::var("OUT_DIR").expect("OUT_DIR"));
    let target_env = env::var("CARGO_CFG_TARGET_ENV").unwrap_or_default();
    if target_env == "msvc" {
        let res_path = out_dir.join("msshi-doctor-icon.res");
        fs::write(&res_path, build_msvc_res(&parsed)).expect("Failed to write .res");
        println!("cargo:rustc-link-arg-bins={}", res_path.display());
    } else {
        let arch = env::var("CARGO_CFG_TARGET_ARCH").unwrap_or_else(|_| "x86_64".into());
        let obj_path = out_dir.join("msshi-doctor-icon.o");
        fs::write(&obj_path, build_gnu_coff_obj(&parsed, &arch)).expect("Failed to write COFF icon object");
        println!("cargo:rustc-link-arg-bins={}", obj_path.display());
    }
}
