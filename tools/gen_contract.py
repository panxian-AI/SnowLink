"""从 SnowLuma 发行包重新生成 snowlink/contract.py。

用法：

    python tools/gen_contract.py /path/to/snow            # 目录（含 index.mjs）
    python tools/gen_contract.py /path/to/snow/index.mjs  # 或直接给文件

它只读发行包里的**接口定义**（动作名、参数名、retcode 数值），不复制实现代码；
每个动作归到一个能力族，族的原因文案写在本文件的 REASONS 里（本项目自写）。

SnowLuma 升级后如果新增了动作，这个脚本会直接报出来「这几个动作还没归类」，
按提示补进 FAMILIES 再跑一次即可。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

KINDS = ("defineAction", "groupAction", "groupUserAction", "defineStreamAction")

#: 真能执行的动作（与 onebot.py 的 _handlers() 对应）
CORE = [
    "send_private_msg", "send_group_msg", "send_msg",
    "get_login_info", "get_status", "get_version_info",
    "get_friend_list", "get_group_list", "get_group_info",
    "get_msg", "can_send_image", "can_send_record",
]

FAMILIES: dict[str, list[str]] = {
    "credentials": [
        "get_cookies", "get_csrf_token", "get_credentials", "get_clientkey",
        "get_rkey_server", "request_decrypt_key",
    ],
    "online": [
        "get_online_clients", "set_online_status", "set_diy_online_status",
        "set_input_status", "nc_get_packet_status", "nc_get_user_status",
    ],
    "control": ["bot_exit", "set_restart", "clean_cache", "clean_stream_temp_file"],
    "read": [
        "mark_group_msg_as_read", "mark_msg_as_read", "mark_private_msg_as_read",
        "_mark_all_as_read",
    ],
    "recall": ["delete_msg"],
    "media": ["get_image", "get_record"],
    "history": ["get_group_msg_history", "get_friend_msg_history", "get_forward_msg"],
    "friend": [
        "get_stranger_info", "get_unidirectional_friend_list", "get_friends_with_category",
        "get_recent_contact", "delete_friend", "set_friend_remark", "set_friends_category",
        "get_doubt_friends_add_request", "set_doubt_friends_add_request",
        "get_profile_like", "_get_friend_dress", "set_friend_add_request",
    ],
    "group_query": [
        "get_group_info_ex", "get_group_detail_info", "get_group_member_info",
        "get_group_member_list", "get_group_honor_info", "get_group_shut_list",
        "get_group_signed_list", "get_group_system_msg", "get_group_admin_settings",
        "get_group_admin_permissions", "get_group_at_all_remain", "get_group_tags",
        "get_group_share_link", "get_group_ignored_notifies", "get_group_ignore_add_request",
        "get_group_todo_list", "share_group_ex", "get_essence_msg_list",
    ],
    "group_mgmt": [
        "set_group_card", "set_group_name", "set_group_ban", "set_group_whole_ban",
        "set_group_kick", "set_group_kick_members", "set_group_leave", "set_group_admin",
        "set_group_admin_permissions", "set_group_admin_receive_verify",
        "set_group_anonymous", "set_group_anonymous_ban", "set_group_add_option",
        "set_group_add_request", "set_group_member_invite_policy",
        "set_group_member_permissions", "set_group_new_member_history_visibility",
        "set_group_portrait", "set_group_reaction", "set_group_remark",
        "set_group_robot_add_option", "set_group_search", "set_group_special_title",
        "add_group_tag", "delete_group_tag", "delete_essence_msg", "set_essence_msg",
        "set_group_todo", "cancel_group_todo", "complete_group_todo",
        "_get_group_notice", "_send_group_notice", "_del_group_notice",
    ],
    "file": [
        "download_file", "download_file_image_stream", "download_file_record_stream",
        "download_file_stream", "download_fileset", "get_file", "get_fileset_id",
        "get_fileset_info", "get_flash_file_list", "get_flash_file_url",
        "get_private_file_url", "get_group_file_system_info", "get_group_file_url",
        "get_group_files_by_folder", "get_group_root_files", "create_group_file_folder",
        "delete_group_file", "delete_group_file_folder", "delete_group_folder",
        "move_group_file", "rename_group_file", "rename_group_file_folder",
        "trans_group_file", "upload_group_file", "upload_private_file", "list_filesets",
        "delete_flash_file", "rename_flash_file", "upload_file_stream",
        "test_download_stream", "send_flash_msg", "create_flash_task",
    ],
    "album": [
        "get_group_album_list", "get_group_album_media_list", "get_qun_album_list",
        "cancel_group_album_media_like", "del_group_album_media", "do_group_album_comment",
        "set_group_album_media_like", "upload_image_to_qun_album",
        "upload_video_to_qun_album",
    ],
    "qzone": [
        "comment_qzone", "comment_qzone_public_feed", "delete_qzone_msg",
        "delete_qzone_public_comment", "delete_qzone_public_reply",
        "forward_qzone_public_feed", "get_qzone_feeds", "get_qzone_msg_detail",
        "get_qzone_msg_list", "get_qzone_profile", "get_qzone_public_capabilities",
        "get_qzone_public_comments", "get_qzone_public_feed_detail",
        "get_qzone_public_feeds", "get_qzone_public_replies", "like_qzone",
        "set_qzone_ban", "set_qzone_msg_right", "set_qzone_profile",
        "set_qzone_public_comment_like", "set_qzone_public_follow",
        "set_qzone_public_like", "set_qzone_public_upvote", "send_qzone_msg",
        "unlike_qzone", "reply_qzone_public_feed",
    ],
    "face": [
        "add_custom_face", "delete_custom_face", "fetch_custom_face",
        "fetch_custom_face_detail", "modify_custom_face", "move_custom_face_to_front",
        "send_custom_face", "fetch_emoji_like", "fetch_face_entity",
        "fetch_super_face_id", "fetch_sys_faces", "search_sys_faces",
        "get_emoji_likes", "get_msg_emoji_likes", "set_msg_emoji_like",
    ],
    "forward": [
        "send_forward_msg", "send_group_forward_msg", "send_private_forward_msg",
        "forward_friend_single_msg", "forward_group_single_msg", "upload_forward_msg",
        "upload_foward_msg",
    ],
    "extra": [
        "friend_poke", "group_poke", "send_poke", "send_like", "send_ark_share",
        "send_group_ark_share", "send_tuwen_ark", "get_mini_app_ark",
        "click_inline_keyboard_button", "create_collection", "get_collection_list",
        "get_ai_characters", "get_ai_record", "send_group_ai_record", "translate_en2zh",
        "check_url_safely", "get_share_link", "_get_model_show", "_set_model_show",
        "set_qq_avatar", "set_qq_profile", "set_self_longnick", "share_peer",
    ],
}

#: 能力族 -> 做不到时给上层的原因（本项目自写，会原样进 wording）
REASONS = {
    "credentials": "QQ 登录凭据由 ArkLink 侧的官方客户端持有，OneBot 侧拿不到（这是不注入进程的代价）",
    "online": "在线状态与客户端信息接口未实现；ArkLink 只保证消息收发",
    "control": "进程控制不归本进程管，退出/重启请用 systemd 或 start.bat",
    "read": "没有已读上报通道（ArkLink 能力矩阵里 read_report=false）",
    "recall": "撤回需要 ArkLink 的 recall 能力，矩阵里是 false",
    "media": "不接管图片/语音文件本体；ArkLink 只传文本",
    "history": "历史消息接口未实现；ArkLink 只提供本次运行内的消息缓冲（since 续传）",
    "friend": "好友资料/好友管理接口还没落地（需要 ArkLink 的花名册接口）",
    "group_query": "群资料与成员查询还没接上真实 QQ（ArkLink 能力矩阵里 roster=false）",
    "group_mgmt": "群管理动作还没落地（ArkLink 能力矩阵里 group_manage=false）",
    "file": "群文件/私聊文件接口未实现（需要 ArkLink 的文件通道）",
    "album": "群相册接口未实现（需要 ArkLink 的相册通道）",
    "qzone": "QQ 空间接口未实现（需要 ArkLink 的空间通道）",
    "face": "表情与自定义表情接口未实现（需要 ArkLink 的结构化消息通道）",
    "forward": "合并转发需要结构化消息通道；ArkLink 现在只能发纯文本",
    "extra": "点赞/戳一戳/分享卡片/小程序/收藏等扩展接口未实现（需要 ArkLink 的结构化消息通道）",
}


def brace_block(s: str, open_idx: int) -> str:
    depth = 0
    for i in range(open_idx, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return s[open_idx : i + 1]
    return s[open_idx : open_idx + 3000]


def extract_actions(bundle: Path) -> list[str]:
    text = bundle.read_text(encoding="utf-8", errors="replace")
    names: list[str] = []
    for m in re.finditer(r"\b(" + "|".join(KINDS) + r")\(\{", text):
        block = brace_block(text, text.index("{", m.start()))[:8000]
        nm = re.search(r'\bname:\s*"([A-Za-z0-9_]+)"', block)
        if nm and nm.group(1) not in names:
            names.append(nm.group(1))
    return names


def find_bundle(arg: str) -> Path:
    p = Path(arg)
    if p.is_dir():
        p = p / "index.mjs"
    if not p.is_file():
        raise SystemExit(f"找不到 {p}：给发行包目录或其中的 index.mjs")
    return p


def build(names: list[str]) -> tuple[dict[str, str], list[str]]:
    flat: dict[str, str] = {n: "core" for n in CORE}
    for fam, items in FAMILIES.items():
        for n in items:
            if n in flat:
                raise SystemExit(f"动作 {n} 被重复归类")
            flat[n] = fam
    missing = sorted(set(names) - set(flat))
    stale = sorted(set(flat) - set(names))
    return flat, (missing or []) + ([] if not stale else [f"（旧版遗留，可删）{s}" for s in stale])


def render(flat: dict[str, str], count: int) -> str:
    lines = [
        '"""SnowLuma 的 OneBot v11 动作面（自动生成，勿手改）。',
        "",
        "动作名与 retcode 数值来自 SnowLuma 发行包的接口定义（可观测契约）；",
        "每个动作归到一个「能力族」，会话内做不到的族统一给 1404 + 一句为什么。",
        "重新生成：python tools/gen_contract.py <发行包目录或 index.mjs>",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "# OneBot v11 的 retcode 取值，与 SnowLuma 的枚举一一对应",
        "RET_OK = 0",
        "RET_ACTION_FAILED = 100",
        "RET_INTERNAL = 1200",
        "RET_BAD_REQUEST = 1400",
        "RET_UNKNOWN_ACTION = 1404",
        "",
        f"ACTION_COUNT = {count}",
        "",
        "#: 动作名 -> 能力族",
        "ACTIONS: dict[str, str] = {",
    ]
    for name in sorted(flat):
        lines.append(f'    "{name}": "{flat[name]}",')
    lines += ["}", "", "#: 能力族 -> 做不到时的原因（会原样进响应的 wording）", "REASONS: dict[str, str] = {"]
    for fam in sorted(REASONS):
        lines.append(f'    "{fam}": {json.dumps(REASONS[fam], ensure_ascii=False)},')
    lines += [
        "}",
        "",
        "",
        "def family_of(action: str) -> str:",
        '    """动作属于哪个能力族；不认识的动作返回空串。"""',
        '    return ACTIONS.get(action, "")',
        "",
        "",
        "def reason_of(action: str) -> str:",
        '    """这个动作为什么做不到（人话）；不认识的动作返回空串。"""',
        '    return REASONS.get(ACTIONS.get(action, ""), "")',
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    bundle = find_bundle(argv[1])
    names = extract_actions(bundle)
    flat, problems = build(names)
    if problems:
        print("归类不完整，先补齐再生成：")
        for p in problems:
            print("  ", p)
        return 1
    out = Path(__file__).resolve().parent.parent / "snowlink" / "contract.py"
    out.write_text(render(flat, len(names)), encoding="utf-8", newline="\n")
    print(f"{len(names)} 个动作 -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
