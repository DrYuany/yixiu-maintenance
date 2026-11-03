import sys
from pathlib import Path

# yxcore 在父目录，测试从这里引入；不加会出现「能 import 但路径飘」的问题。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
