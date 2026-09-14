"""ChannelFlow AI - Keyboards.

This module used to hold ~50 keyboard builders describing a second,
older UI. None of them were called. Every live screen builds its
keyboard inline in the handler that owns it, so this file had become a
parallel description of an interface that no longer existed - and the
two had drifted apart, badly enough that its payment flow emitted
callbacks (`pay:method`, `pay:create:`, `pay:durations:`) that no
handler implemented at all.

It was also actively misleading: the sweep reported its buttons as
reachable because they *looked* wired in, and `settings_keyboard()`
carried a comment about fixing a crash in `handlers.py` long after
`handlers.py` had stopped importing it.

What remains is the one thing still imported: the language picker. It
lives here because each language is shown in its own script by design -
that is the only place in the bot where a non-English string is
deliberately hardcoded.

Older messages already sent to users still carry buttons whose callbacks
this module used to produce (`platform:telegram`, `pay:method`,
`newproj`, `support:*`). Those handlers stay in bot/handlers_nav.py and
bot/handlers_features.py as aliases, because Telegram messages persist
and tapping an old button must still do the right thing.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# Each language is deliberately shown in its own script: a language
# picker is the one screen where translating the label is wrong.
LANGUAGE_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("English 🇬🇧", callback_data="lang:en")],
    [InlineKeyboardButton("हिंदी 🇮🇳", callback_data="lang:hi")],
    [InlineKeyboardButton("বাংলা 🇧🇩", callback_data="lang:bn")],
    [InlineKeyboardButton("اردو 🇵🇰", callback_data="lang:ur")],
    [InlineKeyboardButton("Español 🇪🇸", callback_data="lang:es")],
    [InlineKeyboardButton("العربية 🇸🇦", callback_data="lang:ar")],
    [InlineKeyboardButton("Bahasa Indonesia 🇮🇩", callback_data="lang:id")],
    [InlineKeyboardButton("⬅ Back to Settings", callback_data="nav:settings")],
])
