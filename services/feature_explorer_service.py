"""
ChannelFlow AI - Feature Explorer Catalog Service
Implements PRD §5.4: Plan-by-Plan Feature Explorer with pagination.
"""
from typing import Dict, List, Tuple

PLAN_FEATURES: Dict[str, List[Dict[str, str]]] = {
    "FREE": [
        {"id": "f_fwd", "name": "🚀 Auto Forwarding", "desc": "Telegram-to-Telegram automated message relaying."},
        {"id": "f_proj", "name": "📁 Up to 3 Projects", "desc": "Run up to 3 separate automated forwarding pipelines."},
        {"id": "f_src", "name": "📥 10 Sources per Project", "desc": "Connect up to 10 channels/groups as post sources."},
        {"id": "f_tgt", "name": "🎯 10 Targets per Project", "desc": "Forward messages into up to 10 destination channels."},
        {"id": "f_quota", "name": "📈 100 Daily Forwards", "desc": "Daily forwarding quota for personal workflows."},
        {"id": "f_text", "name": "✍️ Text & Caption Sync", "desc": "Relay post captions and standalone text seamlessly."},
        {"id": "f_media", "name": "🖼️ Standard Media Forwarding", "desc": "Support for photos, documents, and standard videos."},
        {"id": "f_delay", "name": "⏱️ Configurable Delay", "desc": "Add a fixed or randomized delay before forwarding."},
        {"id": "f_kw", "name": "🔑 Whitelist Keywords", "desc": "Forward messages only if they contain required keywords."},
        {"id": "f_bl", "name": "🚫 Blacklist Keywords", "desc": "Skip posts containing unwanted spam keywords."},
        {"id": "f_status", "name": "📊 Project Status & Logs", "desc": "Inspect real-time forwarding logs and active health state."}
    ],
    "STARTER": [
        {"id": "s_proj", "name": "📁 Up to 7 Projects", "desc": "Expand your capacity to 7 concurrent forwarding projects."},
        {"id": "s_src", "name": "📥 25 Sources per Project", "desc": "Aggregate content across up to 25 channels or groups."},
        {"id": "s_tgt", "name": "🎯 25 Targets per Project", "desc": "Distribute posts simultaneously to 25 targets."},
        {"id": "s_quota", "name": "📈 200 Daily Forwards", "desc": "Double daily forwarding quota across your projects."},
        {"id": "s_noattr", "name": "🏷️ No Attribution Footer", "desc": "Clean forwarding without bot attribution notices."},
        {"id": "s_edit", "name": "🔄 Post Edit Sync", "desc": "Edits made in source channels are mirrored to destinations."},
        {"id": "s_react", "name": "❤️ Auto Reactions", "desc": "Automatically react with specified emojis to eligible posts."},
        {"id": "s_topics", "name": "🧵 Forum Topic Forwarding", "desc": "Forward specific threads from forum-enabled supergroups."},
        {"id": "s_mono", "name": "💻 Monospace / Code Text", "desc": "Transform forwarded text into clean monospace styling."},
        {"id": "s_links", "name": "🔗 Link Preview Control", "desc": "Enable or disable web link previews dynamically."},
        {"id": "s_trim", "name": "✂️ Trim Words & Lines", "desc": "Remove headers, footers, or first/last N lines cleanly."},
        {"id": "s_headfoot", "name": "📝 Custom Header & Footer", "desc": "Attach personalized signatures or branding to all posts."}
    ],
    "PRO": [
        {"id": "p_proj", "name": "📁 Up to 15 Projects", "desc": "Professional capacity: 15 active automation projects."},
        {"id": "p_src", "name": "📥 50 Sources per Project", "desc": "Massive aggregation: listen to 50 sources simultaneously."},
        {"id": "p_tgt", "name": "🎯 50 Targets per Project", "desc": "Broadcasting power: deliver into 50 destination channels."},
        {"id": "p_quota", "name": "📈 1,000 Daily Forwards", "desc": "Heavy-duty 1,000 forwards/day allowance."},
        {"id": "p_ai", "name": "🤖 AI Post Rewriter", "desc": "Intelligently rewrite captions with customizable tones via OpenRouter."},
        {"id": "p_wm", "name": "🖼️ Custom Watermarking", "desc": "Burn custom text or logo watermarks onto photos and images."},
        {"id": "p_aff", "name": "💰 Affiliate Link Replacer", "desc": "Auto-convert Amazon, Flipkart, & Meesho links to your tag."},
        {"id": "p_userstrip", "name": "👤 Strip Usernames & Handles", "desc": "Automatically strip competing @channel mentions."},
        {"id": "p_hidelink", "name": "🛡️ Strip Hidden Hyperlinks", "desc": "Detect and clean hidden text link entities."},
        {"id": "p_regex", "name": "🔤 Advanced Regex Filter", "desc": "Filter and match content using custom regular expressions."},
        {"id": "p_pacing", "name": "⚡ Safe Pacing & Anti-Flood", "desc": "Automated throttling to protect accounts against Telegram rate limits."}
    ],
    "CREATOR": [
        {"id": "c_quota", "name": "📈 2,000+ Daily Forwards", "desc": "Enterprise tier forwarding allowance for large creator networks."},
        {"id": "c_dest_override", "name": "🎯 Per-Destination Formatting", "desc": "Customize separate headers/footers for each distinct target."},
        {"id": "c_speed", "name": "⚡ High-Throughput Engine", "desc": "Priority execution queue with parallel destination workers."},
        {"id": "c_vip", "name": "👑 Direct Owner Support", "desc": "Direct priority contact channel with ChannelFlow platform operators."},
        {"id": "c_all_inclusive", "name": "💎 Unlimited Access", "desc": "Full, unrestricted access to all present and future automation tools."}
    ]
}

PLAN_SYMBOLS = {
    "FREE": "🆓 Free",
    "STARTER": "🚀 Starter",
    "PRO": "⭐ Pro",
    "CREATOR": "👑 Creator"
}

PAGE_SIZE = 8

def get_plan_features(plan_name: str) -> List[Dict[str, str]]:
    hierarchy = ["FREE", "STARTER", "PRO", "CREATOR"]
    plan_upper = plan_name.upper()
    if plan_upper not in hierarchy:
        plan_upper = "FREE"
    features = []
    for p in hierarchy:
        features.extend(PLAN_FEATURES.get(p, []))
        if p == plan_upper:
            break
    return features

def get_plan_page(plan_name: str, page: int = 0) -> Tuple[List[Dict[str, str]], int, int]:
    features = get_plan_features(plan_name)
    total_items = len(features)
    total_pages = max(1, (total_items + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start_idx = page * PAGE_SIZE
    end_idx = start_idx + PAGE_SIZE
    return features[start_idx:end_idx], page, total_pages

def get_feature_by_id(feature_id: str) -> Dict[str, str]:
    for plan, f_list in PLAN_FEATURES.items():
        for f in f_list:
            if f["id"] == feature_id:
                return {**f, "min_plan": PLAN_SYMBOLS[plan]}
    return {"name": "Feature", "desc": "Feature details currently unavailable.", "min_plan": "Pro"}