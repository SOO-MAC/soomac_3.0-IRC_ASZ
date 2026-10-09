#!/usr/bin/env python3
"""Generate CLOVA protobuf bindings next to nest.proto, independent of cwd."""
from pathlib import Path


def main():
    try:
        from grpc_tools import protoc
    except ImportError:
        raise SystemExit(
            "생성 도구가 필요합니다: python3 -m pip install grpcio-tools==1.76.0"
        )
    directory = Path(__file__).resolve().parent
    result = protoc.main([
        "grpc_tools.protoc",
        f"-I{directory}",
        f"--python_out={directory}",
        f"--grpc_python_out={directory}",
        str(directory / "nest.proto"),
    ])
    if result:
        raise SystemExit(result)
    print(f"생성 완료: {directory / 'nest_pb2.py'}, {directory / 'nest_pb2_grpc.py'}")


if __name__ == "__main__":
    main()
