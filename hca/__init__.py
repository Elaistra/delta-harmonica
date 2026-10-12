"""hca —— 三角洲口琴自动演奏（自研演奏程序）

模块地图：
    mapping.py   键位映射（可插拔 JSON 配置）★ 换游戏只改这里
    model.py     数据结构 + JS 式取整
    midi.py      MIDI 解析（规则由黑盒探针实测得到）
    abc.py       ABC 解析（尚未实现，见文件内说明）
    library.py   扫曲库 / 按内容载入乐谱
    inputs.py    输入注入（SendInput / 空后端 / 光标归位）
    player.py    播放调度（可暂停、可变速）
    hotkeys.py   全局热键
    config.py    配置读写
    reference.py 旧内核适配器（只用于对照验证）
    cli.py       命令行入口
"""

__version__ = "0.1.0-dev"
