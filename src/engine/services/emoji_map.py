"""WeChat emoji text-code to Unicode emoji mapping.

Covers the standard WeChat emoji set used in chat messages as [xxx] codes.
Based on the well-documented WeChat emoji standard (100+ expressions).
"""

WECHAT_EMOJI_MAP = {
    # === Face expressions ===
    '[微笑]': '\U0001f60a',       # 😊 smiling face
    '[撇嘴]': '\U0001f623',       # 😣 persevering face
    '[色]': '\U0001f60d',         # 😍 heart eyes
    '[发呆]': '\U0001f633',       # 😳 flushed face
    '[得意]': '\U0001f60e',       # 😎 sunglasses
    '[流泪]': '\U0001f622',       # 😢 crying face
    '[害羞]': '\U0001f60a',       # 😊 smiling face (blush)
    '[闭嘴]': '\U0001f910',       # 🤐 zipper mouth
    '[睡]': '\U0001f634',         # 😴 sleeping face
    '[大哭]': '\U0001f62d',       # 😭 loudly crying
    '[尴尬]': '\U0001f605',       # 😅 grinning with sweat
    '[发怒]': '\U0001f621',       # 😡 pouting face
    '[调皮]': '\U0001f61c',       # 😜 winking with tongue
    '[呲牙]': '\U0001f601',       # 😁 beaming face
    '[惊讶]': '\U0001f632',       # 😲 astonished face
    '[难过]': '\U0001f614',       # 😔 pensive face
    '[酷]': '\U0001f60e',         # 😎 sunglasses
    '[冷汗]': '\U0001f630',       # 😰 anxious with sweat
    '[抓狂]': '\U0001f62b',       # 😫 tired face
    '[吐]': '\U0001f92e',         # 🤮 vomiting
    '[偷笑]': '\U0001f92d',       # 🤭 hand over mouth
    '[愉快]': '\U0001f60a',       # 😊
    '[白眼]': '\U0001f644',       # 🙄 eye roll
    '[傲慢]': '\U0001f624',       # 😤 steam from nose
    '[饥饿]': '\U0001f924',       # 🤤 drooling
    '[困]': '\U0001f62a',         # 😪 sleepy face
    '[惊恐]': '\U0001f631',       # 😱 screaming in fear
    '[流汗]': '\U0001f613',       # 😓 downcast with sweat
    '[憨笑]': '\U0001f604',       # 😄 grinning
    '[悠闲]': '\U0001f60c',       # 😌 relieved
    '[大兵]': '\U0001fae1',       # 🫡 salute (newer WeChat)
    '[奋斗]': '\U0001f4aa',       # 💪 flexed biceps
    '[咒骂]': '\U0001f92c',       # 🤬 swearing
    '[疑问]': '\U00002753',       # ❓ question mark
    '[嘘]': '\U0001f92b',         # 🤫 shushing
    '[晕]': '\U0001f635',         # 😵 dizzy face
    '[疯了]': '\U0001f92a',       # 🤪 zany face
    '[衰]': '\U0001f61e',         # 😞 disappointed
    '[骷髅]': '\U0001f480',       # 💀 skull
    '[敲打]': '\U0001f44a',       # 👊 oncoming fist
    '[再见]': '\U0001f44b',       # 👋 waving hand
    '[擦汗]': '\U0001f605',       # 😅
    '[抠鼻]': '\U0001f443',       # 👃 nose (or 🤏)
    '[鼓掌]': '\U0001f44f',       # 👏 clapping
    '[糗大了]': '\U0001f616',     # 😖 confounded
    '[坏笑]': '\U0001f60f',       # 😏 smirking
    '[左哼哼]': '\U0001f624',     # 😤
    '[右哼哼]': '\U0001f624',     # 😤
    '[哈欠]': '\U0001f971',       # 🥱 yawning
    '[鄙视]': '\U0001f612',       # 😒 unamused
    '[委屈]': '\U0001f97a',       # 🥺 pleading
    '[快哭了]': '\U0001f979',     # 🥹 holding back tears
    '[阴险]': '\U0001f608',       # 😈 smirking with horns
    '[亲亲]': '\U0001f618',       # 😘 blowing a kiss
    '[吓]': '\U0001f628',         # 😨 fearful
    '[可怜]': '\U0001f97a',       # 🥺

    # === Gesture / hand ===
    '[菜刀]': '\U0001f52a',       # 🔪 kitchen knife
    '[西瓜]': '\U0001f349',       # 🍉 watermelon
    '[啤酒]': '\U0001f37a',       # 🍺 beer
    '[篮球]': '\U0001f3c0',       # 🏀 basketball
    '[乒乓]': '\U0001f3d3',       # 🏓 ping pong
    '[咖啡]': '\U00002615',       # ☕ hot beverage
    '[饭]': '\U0001f35a',         # 🍚 cooked rice
    '[猪头]': '\U0001f437',       # 🐷 pig face
    '[玫瑰]': '\U0001f339',       # 🌹 rose
    '[凋谢]': '\U0001f940',       # 🥀 wilted flower
    '[嘴唇]': '\U0001f48b',       # 💋 kiss mark
    '[爱心]': '\U00002764\U0000fe0f',  # ❤️ red heart
    '[心碎]': '\U0001f494',       # 💔 broken heart
    '[蛋糕]': '\U0001f382',       # 🎂 birthday cake
    '[闪电]': '\U000026a1',       # ⚡ high voltage
    '[炸弹]': '\U0001f4a3',       # 💣 bomb
    '[刀]': '\U0001f52a',         # 🔪
    '[足球]': '\U000026bd',       # ⚽ soccer ball
    '[瓢虫]': '\U0001f41e',       # 🐞 lady beetle
    '[便便]': '\U0001f4a9',       # 💩 pile of poo
    '[月亮]': '\U0001f319',       # 🌙 crescent moon
    '[太阳]': '\U00002600\U0000fe0f',  # ☀️ sun
    '[礼物]': '\U0001f381',       # 🎁 wrapped gift
    '[拥抱]': '\U0001f917',       # 🤗 hugging
    '[强]': '\U0001f44d',         # 👍 thumbs up
    '[弱]': '\U0001f44e',         # 👎 thumbs down
    '[握手]': '\U0001f91d',       # 🤝 handshake
    '[胜利]': '\U0000270c\U0000fe0f',  # ✌️ victory hand
    '[抱拳]': '\U0001f64f',       # 🙏 folded hands
    '[勾引]': '\U0001f4aa',       # 💪 (flex)
    '[拳头]': '\U0000270a',       # ✊ raised fist
    '[差劲]': '\U0001f44e',       # 👎
    '[爱你]': '\U0001f91f',       # 🤟 love-you gesture
    '[NO]': '\U0001f645',         # 🙅 no gesture
    '[OK]': '\U0001f646',         # 🙆 OK gesture
    '[爱情]': '\U0001f491',       # 💑 couple with heart
    '[飞吻]': '\U0001f618',       # 😘
    '[跳跳]': '\U0001f938',       # 🤸 cartwheel
    '[发抖]': '\U0001f976',       # 🥶 cold face
    '[怄火]': '\U0001f620',       # 😠 angry
    '[转圈]': '\U0001f300',       # 🌀 cyclone
    '[磕头]': '\U0001f647',       # 🙇 bowing
    '[回头]': '\U0001f481',       # 💁 tipping hand
    '[跳绳]': '\U0001f93e',       # 🤾 handball (approximate)
    '[投降]': '\U0001f64c',       # 🙌 raising hands
    '[激动]': '\U0001f929',       # 🤩 star-struck
    '[街舞]': '\U0001f483',       # 💃 dancer
    '[献吻]': '\U0001f48b',       # 💋
    '[左太极]': '\U0001f91c',     # 🤜 right-facing fist
    '[右太极]': '\U0001f91b',     # 🤛 left-facing fist

    # === Newer WeChat emoji (WeChat 4.x+) ===
    '[嘿哈]': '\U0001f973',       # 🥳 partying face
    '[捂脸]': '\U0001f926',       # 🤦 facepalm
    '[奸笑]': '\U0001f608',       # 😈
    '[机智]': '\U0001f9e0',       # 🧠 brain (witty)
    '[皱眉]': '\U0001f615',       # 😕 confused
    '[耶]': '\U0000270c\U0000fe0f',  # ✌️
    '[红包]': '\U0001f9e7',       # 🧧 red envelope
    '[烟花]': '\U0001f386',       # 🎆 fireworks
    '[爆竹]': '\U0001f9e8',       # 🧨 firecracker
    '[福]': '\U0001f4ef',         # 📯 (blessing)
    '[鸡]': '\U0001f414',         # 🐔 chicken
    '[笑脸]': '\U0001f60a',       # 😊
    '[生病]': '\U0001f637',       # 😷 face with mask
    '[破涕为笑]': '\U0001f602',   # 😂 joy tears
    '[社会社会]': '\U0001f91d',   # 🤝
    '[旺柴]': '\U0001f436',       # 🐶 dog
    '[好的]': '\U0001f44c',       # 👌 OK hand
    '[打脸]': '\U0001f4a5',       # 💥 collision (slap)
    '[哇]': '\U0001f92f',         # 🤯 exploding head
    '[翻白眼]': '\U0001f644',     # 🙄
    '[666]': '\U0001f44d',        # 👍
    '[让我看看]': '\U0001f440',   # 👀 eyes
    '[叹气]': '\U0001f4a8',       # 💨 dashing
    '[苦涩]': '\U0001f922',       # 🤢 nauseated
    '[裂开]': '\U0001f635\U0000200d\U0001f4ab',  # 😵‍💫 face with spiral eyes
    '[吃瓜]': '\U0001f349',       # 🍉

    # === Additional codes from WeChat 4.x data scan ===
    '[分享]': '\U0001f4e4',       # 📤 outbox tray
    '[加油]': '\U0001f4aa',       # 💪 flexed biceps
    '[合十]': '\U0001f64f',       # 🙏 folded hands
    '[囧]': '\U0001f615',         # 😕 confused
    '[天啊]': '\U0001f632',       # 😲 astonished
    '[庆祝]': '\U0001f389',       # 🎉 party popper
    '[恐惧]': '\U0001f631',       # 😱 screaming
    '[无语]': '\U0001f636',       # 😶 speechless
    '[汗]': '\U0001f613',         # 😓 sweat
    '[發]': '\U0001f4b0',         # 💰 money bag
    '[脸红]': '\U0001f633',       # 😳 flushed
    '[失望]': '\U0001f61e',       # 😞 disappointed
}


def translate_wechat_emoji(text: str) -> str:
    """Replace WeChat [xxx] emoji codes with Unicode emoji characters.

    Only replaces codes that are an exact match in the known emoji set.
    Does not touch bracket text that isn't a recognized emoji code.
    """
    if not text:
        return text
    for code, emoji in WECHAT_EMOJI_MAP.items():
        text = text.replace(code, emoji)
    return text
