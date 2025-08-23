import argparse
import sys
from Parser.sfi_file_parser import parse_file, all_functions


def decompile(all_func):
    print(f"Decompiling {len(all_func)} functions.")
    for name in list(all_func.keys()):
        try:
            all_functions[name].decompile()
        except Exception as e:
            # If upstream raises progress info "stopped after X/Y", just forward it
            msg = getattr(e, "args", [""])[0] or str(e)
            if "stopped after" in msg:
                print(f"Warning! failed to decompile {name} {msg}")
            else:
                print(f"Warning! failed to decompile {name}: {e}")


def export_to_file(output_file, all_func, export_format):
    # Ensure we always write UTF-8 to avoid Windows default 'gbk' UnicodeEncodeError
    # newline='\n' for consistent line endings across platforms
    format_list = [s.strip().lower() for s in (export_format or "").split(",") if s.strip()]
    if not format_list:
        format_list = ["decompiled"]

    print(f"Exporting to file {output_file}.")
    with open(output_file, "w", encoding="utf-8", newline="\n") as f:
        for function_name in all_func:
            sfi = all_functions[function_name]
            content = sfi.export(
                export_v8code=("v8_opcode" in format_list),
                export_translated=("translated" in format_list),
                export_decompiled=("decompiled" in format_list),
            )
            # content is plain str; writing with utf-8 avoids 'gbk' encode errors
            f.write(content)
    print("Done.")


def main():
    parser = argparse.ArgumentParser(description="View8 decompiler")
    parser.add_argument("--disassembled", action="store_true", help="Input is V8 disassembled output")
    parser.add_argument("input_file", help="Path to disassembled file")
    parser.add_argument("output_file", help="Path to output JS file")
    parser.add_argument(
        "--export-format",
        default="decompiled",
        help="Comma-separated formats: decompiled,translated,v8_opcode (default: decompiled)",
    )
    args = parser.parse_args()

    print("Parsing disassembled file.")
    all_func = parse_file(args.input_file)
    print("Parsing completed successfully.")

    decompile(all_func)
    export_to_file(args.output_file, all_func, args.export_format)


if __name__ == "__main__":
    # Try to avoid possible Windows stdout/stderr encoding issues when printing diagnostics
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()