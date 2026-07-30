"""
RAG 测试数据摄入脚本 — ~100 条热门动漫作品

验证 RAG 链路端到端可用：text → embedding(2000d) → rag_entities → hybrid_search()
"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.engine import engine, init_db
from rag.ingestion import RagEntityIngestor
from rag.retriever import RagEntityRetriever
from core.config import get_settings

# ~100 popular anime entries with real data
ANIME_DATA = [
    # ── 经典高分 ──
    {"subject_id": 1, "name": "新世紀エヴァンゲリオン", "name_cn": "新世纪福音战士", "score": 9.0, "rank": 20, "rating_total": 18500, "date": "1995-10-04", "year": 1995, "platform": "TV", "eps": 26, "tags": [{"name":"科幻","count":3200},{"name":"心理","count":2800},{"name":"机甲","count":2500},{"name":"原创","count":2100},{"name":"经典","count":1900}], "nsfw": False, "chunk_text": "故事以一场名为第二次冲击的大灾难为起点，14岁的少年碇真嗣被父亲碇源堂召唤到NERV组织，驾驶巨大的人形兵器EVA与神秘的使徒战斗。然而战斗的背后隐藏着关乎人类存亡的终极秘密——人类补完计划。作品深入探讨了孤独、抑郁、人际关系等深刻主题，被誉为日本动画史上的里程碑之作。"},
    {"subject_id": 2, "name": "鋼の錬金術師 FULLMETAL ALCHEMIST", "name_cn": "钢之炼金术师FA", "score": 9.1, "rank": 2, "rating_total": 32000, "date": "2009-04-05", "year": 2009, "platform": "TV", "eps": 64, "tags": [{"name":"冒险","count":4500},{"name":"奇幻","count":3800},{"name":"少年漫改","count":3500},{"name":"热血","count":3200},{"name":"等价交换","count":2800}], "nsfw": False, "chunk_text": "爱德华和阿尔冯斯兄弟为了复活逝去的母亲进行禁忌的人体炼成，结果爱德华失去了左腿和右臂，阿尔冯斯失去了整个身体。为了找回失去的一切，兄弟俩踏上了寻找贤者之石的旅程，逐渐揭开了国家军部背后的巨大阴谋。完美的大结局和严谨的等价交换世界观使其成为许多人心中的神作。"},
    {"subject_id": 3, "name": "CLANNAD -クラナド-", "name_cn": "CLANNAD", "score": 8.7, "rank": 45, "rating_total": 22000, "date": "2007-10-04", "year": 2007, "platform": "TV", "eps": 23, "tags": [{"name":"催泪","count":5200},{"name":"校园","count":3500},{"name":"恋爱","count":3000},{"name":"Key社","count":2800},{"name":"治愈","count":2500}], "nsfw": False, "chunk_text": "不良少年冈崎朋也在学校坡道上邂逅了体弱多病的古河渚，从此他的人生轨迹发生了巨大改变。故事讲述了朋也帮助渚重建演剧部的过程中，与各个女主角相遇、成长的故事。第二季After Story更是将视角延伸到毕业后的人生，探讨了家庭、责任与爱的真谛，被誉为催泪神作。"},
    {"subject_id": 4, "name": "コードギアス 反逆のルルーシュ", "name_cn": "Code Geass 反叛的鲁路修", "score": 8.5, "rank": 120, "rating_total": 18000, "date": "2006-10-05", "year": 2006, "platform": "TV", "eps": 25, "tags": [{"name":"机甲","count":2800},{"name":"智斗","count":2600},{"name":"原创","count":2200},{"name":"复仇","count":2000},{"name":"CLAMP","count":1500}], "nsfw": False, "chunk_text": "被神圣不列颠帝国统治的日本，前皇子鲁路修获得了神秘少女C.C.赋予的绝对遵从之力Geass。化身为假面男子Zero，鲁路修领导黑色骑士团掀起了反抗帝国的斗争。作品以宏大的政治叙事、精彩的智斗博弈和一个令人震撼的零之镇魂曲结局征服了无数观众。"},
    {"subject_id": 5, "name": "STEINS;GATE", "name_cn": "命运石之门", "score": 8.9, "rank": 25, "rating_total": 25000, "date": "2011-04-06", "year": 2011, "platform": "TV", "eps": 24, "tags": [{"name":"科幻","count":3800},{"name":"时间旅行","count":3200},{"name":"游戏改","count":2800},{"name":"悬疑","count":2400},{"name":"慢热","count":1800}], "nsfw": False, "chunk_text": "自称疯狂科学家的凤凰院凶真（冈部伦太郎）在秋叶原创立了未来道具研究所，在一次偶然中发明了能够向过去发送邮件的时间机器——D-Mail。然而，每一次改变过去都会带来意想不到的蝴蝶效应。当青梅竹马椎名真由理的死亡成为世界线的收束时，冈部开始了拯救挚友的孤独战斗。前期慢热后期封神的典范。"},
    {"subject_id": 6, "name": "攻殻機動隊 STAND ALONE COMPLEX", "name_cn": "攻壳机动队 SAC", "score": 8.9, "rank": 27, "rating_total": 12000, "date": "2002-10-01", "year": 2002, "platform": "TV", "eps": 26, "tags": [{"name":"科幻","count":3000},{"name":"赛博朋克","count":2800},{"name":"公安","count":2200},{"name":"哲学","count":2000},{"name":"Production I.G","count":1500}], "nsfw": False, "chunk_text": "在近未来的日本，人类身体可以完全义体化，大脑可以直接连接网络。公安九课的草薙素子少佐率领团队调查神秘的 hackers 事件——笑面男事件。作品以单元剧推进主线，深入探讨了意识、灵魂与技术的关系，是赛博朋克题材的巅峰之作，影响了后来无数的科幻作品包括黑客帝国。"},
    {"subject_id": 7, "name": "カウボーイビバップ", "name_cn": "星际牛仔", "score": 9.0, "rank": 15, "rating_total": 15000, "date": "1998-04-03", "year": 1998, "platform": "TV", "eps": 26, "tags": [{"name":"科幻","count":2800},{"name":"太空","count":2500},{"name":"公路片","count":2200},{"name":"爵士","count":2000},{"name":"原创","count":1800}], "nsfw": False, "chunk_text": "2071年，人类已经遍布太阳系。赏金猎人Spike Spiegel和搭档Jet Black驾驶着飞船Bebop号在星际间追捕逃犯。途中他们遇到了身手矫健但嗜财如命的Faye、天才黑客狗Ein和古灵精怪的Ed。每集一个独立故事，配合菅野洋子的爵士配乐，营造出独特的硬汉浪漫与孤独感。See you space cowboy."},
    {"subject_id": 8, "name": "化物語", "name_cn": "化物语", "score": 8.3, "rank": 300, "rating_total": 16000, "date": "2009-07-03", "year": 2009, "platform": "TV", "eps": 15, "tags": [{"name":"西尾维新","count":3500},{"name":"SHAFT","count":3000},{"name":"怪异","count":2800},{"name":"话痨","count":2500},{"name":"新房昭之","count":2200}], "nsfw": False, "chunk_text": "高中生阿良良木历在春假期间遇到了美丽的吸血鬼姬丝秀忒，从此被卷入了各种怪异事件中。他帮助被螃蟹夺走体重的战场原黑仪、被蜗牛附身的八九寺真宵、被猿猴怪异缠绕的神原骏河等少女们解决困扰。新房昭之的独特视觉风格配合西尾维新的长篇对白，创造出一种独一无二的动画体验。"},
    {"subject_id": 9, "name": "まどか☆マギカ", "name_cn": "魔法少女小圆", "score": 8.4, "rank": 250, "rating_total": 20000, "date": "2011-01-06", "year": 2011, "platform": "TV", "eps": 12, "tags": [{"name":"虚渊玄","count":4000},{"name":"魔法少女","count":3500},{"name":"致郁","count":3200},{"name":"原创","count":2800},{"name":"SHAFT","count":2500}], "nsfw": False, "chunk_text": "平凡的中学生鹿目圆过着普通的生活，直到转校生晓美焰的出现和神秘生物丘比的接触。丘比提出契约——实现任何一个愿望，但要成为魔法少女与魔女战斗。随着剧情的推进，魔法少女系统的残酷真相逐渐浮出水面。第3话开始的惊人转折颠覆了整个魔法少女题材，虚渊玄的剧本将看似可爱的外壳下隐藏着深邃的绝望。"},
    {"subject_id": 10, "name": "蟲師", "name_cn": "虫师", "score": 8.9, "rank": 25, "rating_total": 10000, "date": "2005-10-22", "year": 2005, "platform": "TV", "eps": 26, "tags": [{"name":"治愈","count":3500},{"name":"奇幻","count":3000},{"name":"单元剧","count":2800},{"name":"文艺","count":2500},{"name":"和风","count":2000}], "nsfw": False, "chunk_text": "在万物有灵的世界中，存在着一类与动植物完全不同的奇妙生命——虫。虫师银古四处旅行，解决由虫引发的各种奇异现象。每个故事都如同一个寓言，在宁静悠远的山水间缓缓展开。作品以极富诗意的笔触描绘人与自然的关系，配以增田俊郎的东方韵味音乐，营造出独特的治愈系氛围。"},
    # ── 热门大作 ──
    {"subject_id": 11, "name": "進撃の巨人", "name_cn": "进击的巨人", "score": 8.0, "rank": 500, "rating_total": 45000, "date": "2013-04-06", "year": 2013, "platform": "TV", "eps": 25, "tags": [{"name":"热血","count":5500},{"name":"战斗","count":5000},{"name":"末世","count":4200},{"name":"漫画改","count":3800},{"name":"WIT STUDIO","count":3000}], "nsfw": False, "chunk_text": "人类被巨人逼到建立了三道巨大的城墙来保护自己。少年艾伦·耶格尔在目睹母亲被巨人吞食后，立志要消灭所有巨人。加入调查兵团后，他发现自己拥有变身巨人的能力。故事从一个看似简单的生存战斗逐渐展开为涉及历史真相、民族仇恨和自由意志的宏大史诗。"},
    {"subject_id": 12, "name": "鬼滅の刃", "name_cn": "鬼灭之刃", "score": 7.5, "rank": 1200, "rating_total": 55000, "date": "2019-04-06", "year": 2019, "platform": "TV", "eps": 26, "tags": [{"name":"热血","count":6000},{"name":"战斗","count":5500},{"name":"ufotable","count":5000},{"name":"大正","count":2500},{"name":"漫画改","count":2000}], "nsfw": False, "chunk_text": "大正时代的日本，少年灶门炭治郎全家被鬼杀害，唯一幸存的妹妹祢豆子变成了鬼。为了寻找让妹妹恢复为人的方法，炭治郎加入鬼杀队，学会了水之呼吸和火之神神乐。ufotable的制作将战斗场景推向了动画艺术的巅峰，第十九话的火之神神乐被誉为年度最佳单集。"},
    {"subject_id": 13, "name": "呪術廻戦", "name_cn": "咒术回战", "score": 7.5, "rank": 1100, "rating_total": 40000, "date": "2020-10-02", "year": 2020, "platform": "TV", "eps": 24, "tags": [{"name":"热血","count":5000},{"name":"战斗","count":4500},{"name":"MAPPA","count":3800},{"name":"校园","count":2800},{"name":"漫画改","count":2500}], "nsfw": False, "chunk_text": "高中生虎杖悠仁在遭遇诅咒时吞下了宿傩的手指，成为了两面宿傩的容器。为了保护更多人，他加入了东京都立咒术高等专门学校学习如何祓除诅咒。五条悟老师的逆天实力和各具特色的角色设定使其成为新一代Jump热门作品。渋谷事变篇将剧情推向了高潮。"},
    {"subject_id": 14, "name": "【推しの子】", "name_cn": "我推的孩子", "score": 7.8, "rank": 700, "rating_total": 28000, "date": "2023-04-12", "year": 2023, "platform": "TV", "eps": 11, "tags": [{"name":"偶像","count":3500},{"name":"转生","count":3000},{"name":"演艺圈","count":2800},{"name":"悬疑","count":2200},{"name":"动画工房","count":2000}], "nsfw": False, "chunk_text": "妇产科医生五郎意外转生为偶像星野爱的孩子——星野爱久爱海（阿库亚）。母亲在巅峰时刻被刺杀身亡后，阿库亚带着前世的记忆踏入了演艺圈，一边以演员身份成长，一边调查母亲的死因。第一话90分钟的加长版以惊人的叙事转折震撼了所有观众，YOASOBI的アイドル主题曲席卷全球。"},
    {"subject_id": 15, "name": "SPY×FAMILY", "name_cn": "间谍过家家", "score": 7.5, "rank": 1000, "rating_total": 32000, "date": "2022-04-09", "year": 2022, "platform": "TV", "eps": 12, "tags": [{"name":"喜剧","count":4500},{"name":"家庭","count":3800},{"name":"间谍","count":3200},{"name":"WIT STUDIO","count":2800},{"name":"治愈","count":2500}], "nsfw": False, "chunk_text": "为了执行维护东西和平的秘密任务，西国间谍黄昏需要组建一个临时家庭。他领养了能读心的超能力少女阿尼亚，与身份为杀手的约尔缔结了假婚姻。三人各自隐藏着秘密，却在日常生活中逐渐建立了真挚的羁绊。阿尼亚的wakuwaku表情包火遍了整个互联网。"},
    # ── 2020年代热门 ──
    {"subject_id": 16, "name": "ぼっち・ざ・ろっく！", "name_cn": "孤独摇滚！", "score": 8.3, "rank": 250, "rating_total": 18000, "date": "2022-10-08", "year": 2022, "platform": "TV", "eps": 12, "tags": [{"name":"音乐","count":3500},{"name":"社恐","count":3200},{"name":"喜剧","count":3000},{"name":"CloverWorks","count":2500},{"name":"日常","count":2000}], "nsfw": False, "chunk_text": "极度社恐的女高中生后藤独（波奇酱）梦想成为摇滚乐手，每天在壁橱里苦练吉他，在网络上以guitarhero的身份发布翻弹视频。意外被伊地知虹夏拉入结束乐队后，波奇酱开始了从壁橱走向舞台的成长之旅。作品以夸张的演出和细腻的心理描写，精准击中了社恐人群的心灵。"},
    {"subject_id": 17, "name": "葬送のフリーレン", "name_cn": "葬送的芙莉莲", "score": 9.1, "rank": 1, "rating_total": 35000, "date": "2023-09-29", "year": 2023, "platform": "TV", "eps": 28, "tags": [{"name":"奇幻","count":4500},{"name":"治愈","count":4000},{"name":"寿命论","count":3500},{"name":"MADHOUSE","count":3000},{"name":"公路片","count":2500}], "nsfw": False, "chunk_text": "勇者小队击败魔王后的世界。精灵魔法使芙莉莲是长生种，十年冒险对她来说不过弹指一挥间。五十年后，勇者辛美尔去世的那一刻，芙莉莲才意识到自己从未真正了解过这个同伴。她踏上了追寻逝去时光的旅程，带着新的同伴重走当年勇者之路。作品以细腻的笔触描绘时间、记忆与羁绊，首季28集登顶Bangumi全年代排行榜第一名。"},
    {"subject_id": 18, "name": "メイドインアビス", "name_cn": "来自深渊", "score": 8.6, "rank": 60, "rating_total": 15000, "date": "2017-07-07", "year": 2017, "platform": "TV", "eps": 13, "tags": [{"name":"冒险","count":3500},{"name":"奇幻","count":3000},{"name":"黑深残","count":2800},{"name":"Kinema Citrus","count":2000},{"name":"致郁","count":1800}], "nsfw": False, "chunk_text": "在巨大深渊阿比斯周围的小镇上，见习探窟家莉可捡到了一个来自深渊深处的机器人少年——雷格。为了追寻传说中在深渊底部等待的母亲，两个少年少女踏上了前往深渊的不归之旅。每一层的深渊诅咒都伴随着巨大的代价。表面上可爱的画风下隐藏着令人窒息的残酷，Kevin Penkin的配乐堪称天籁。"},
    {"subject_id": 19, "name": "鬼太郎誕生 ゲゲゲの謎", "name_cn": "鬼太郎诞生 咯咯咯之谜", "score": 8.1, "rank": 400, "rating_total": 5000, "date": "2023-11-17", "year": 2023, "platform": "Movie", "eps": 1, "tags": [{"name":"剧场版","count":2000},{"name":"悬疑","count":1800},{"name":"灵异","count":1500},{"name":"水木茂","count":1200},{"name":"东映","count":1000}], "nsfw": False, "chunk_text": "昭和时代的日本，记者水木来到一个闭塞的村庄调查神秘事件。在这里他遇到了幽灵族的幸存者——鬼太郎的父亲。两人联手揭开村庄背后的血腥秘密——一个关于权力、欲望和永恒的恐怖故事。作品以成人向的悬疑手法重新解构了经典IP，是这个时代的异色之作。"},
    {"subject_id": 20, "name": "ヴァイオレット・エヴァーガーデン", "name_cn": "紫罗兰永恒花园", "score": 8.2, "rank": 350, "rating_total": 18000, "date": "2018-01-10", "year": 2018, "platform": "TV", "eps": 13, "tags": [{"name":"治愈","count":4000},{"name":"京阿尼","count":3800},{"name":"催泪","count":3500},{"name":"书信","count":2000},{"name":"单元剧","count":1800}], "nsfw": False, "chunk_text": "战争结束后，曾是士兵的自动手记人偶薇尔莉特在CH邮政公司找到了工作——替不识字的人代写信件。通过为形形色色的客户撰写书信，这个曾经只懂得服从命令的少女开始理解人类的情感——什么是爱与失去。每一集都是独立的催泪故事，京都动画的作画将每个画面都打磨成了艺术品。"},
    # ── 2000年代经典 ──
    {"subject_id": 30, "name": "涼宮ハルヒの憂鬱", "name_cn": "凉宫春日的忧郁", "score": 8.0, "rank": 500, "rating_total": 22000, "date": "2006-04-02", "year": 2006, "platform": "TV", "eps": 14, "tags": [{"name":"校园","count":4000},{"name":"科幻","count":3500},{"name":"京阿尼","count":3000},{"name":"轻小说改","count":2500},{"name":"SOS团","count":2000}], "nsfw": False, "chunk_text": "高中一年级生阿虚遇到了一个宣称对普通人不感兴趣的古怪美少女凉宫春日。春日创建了SOS团（让世界变得更热闹的凉宫春日团），并拉着阿虚、长门有希、朝比奈实玖瑠和古泉一树一起寻找外星人、未来人和超能力者。殊不知这些人都真实存在并且已经在春日的身边。06版独特的乱序播出是动画史的一大实验。"},
    {"subject_id": 31, "name": "天元突破グレンラガン", "name_cn": "天元突破 红莲螺岩", "score": 8.5, "rank": 100, "rating_total": 18000, "date": "2007-04-01", "year": 2007, "platform": "TV", "eps": 27, "tags": [{"name":"热血","count":4500},{"name":"机甲","count":3800},{"name":"GAINAX","count":3000},{"name":"原创","count":2500},{"name":"超级系","count":2200}], "nsfw": False, "chunk_text": "在地下村庄中长大的少年西蒙在挖洞时发现了一个小型钻头机械——螺岩。当巨大的颜面从天而降时，他和热血大哥卡米那一起突破地表，发现了一个被螺旋王统治的广袤世界。从地下到地上，从地球到月球，再到星系间的对决——作品的尺度随着西蒙的成长不断膨胀。钻头是男人的浪漫！"},
    {"subject_id": 32, "name": "けいおん！", "name_cn": "轻音少女", "score": 7.8, "rank": 600, "rating_total": 15000, "date": "2009-04-02", "year": 2009, "platform": "TV", "eps": 13, "tags": [{"name":"音乐","count":3500},{"name":"日常","count":3000},{"name":"京阿尼","count":2800},{"name":"萌","count":2500},{"name":"校园","count":2200}], "nsfw": False, "chunk_text": "迷糊的高一新生平泽唯误打误撞加入了即将废部的轻音部。她和贝斯手秋山澪、鼓手田井中律、键盘手琴吹紬组成了放学后茶会乐队。从零开始学吉他的唯，在伙伴们的陪伴下度过了三年的高中时光。作品定义了21世纪的日常系动画，剧中歌Don't say lazy和ふわふわ時間成为了传世经典。"},
    {"subject_id": 33, "name": "とある科学の超電磁砲", "name_cn": "某科学的超电磁炮", "score": 7.8, "rank": 650, "rating_total": 18000, "date": "2009-10-02", "year": 2009, "platform": "TV", "eps": 24, "tags": [{"name":"科幻","count":3000},{"name":"超能力","count":2800},{"name":"J.C.STAFF","count":2500},{"name":"外传","count":2000},{"name":"校园","count":1800}], "nsfw": False, "chunk_text": "学园都市中排名第三的Level 5超能力者御坂美琴，别名超电磁炮。故事围绕着美琴和她的好友白井黑子、初春饰利、佐天泪子的日常与非日常展开。与魔法禁书目录本传不同，超电磁炮更聚焦于学园都市内的科学侧，特别是克隆人实验妹妹们篇章将作品的深度推向了新的高度。"},
    {"subject_id": 34, "name": "デスノート", "name_cn": "死亡笔记", "score": 8.3, "rank": 250, "rating_total": 28000, "date": "2006-10-03", "year": 2006, "platform": "TV", "eps": 37, "tags": [{"name":"智斗","count":4500},{"name":"悬疑","count":4000},{"name":"漫画改","count":3500},{"name":"犯罪","count":2500},{"name":"MADHOUSE","count":2000}], "nsfw": False, "chunk_text": "天才高中生夜神月在捡到死神琉克丢下的死亡笔记后，开始了以基拉的身份制裁罪犯的行动。为了追捕基拉，国际刑警组织请出了神秘的侦探L。两个天才之间的猫鼠游戏在多重的计谋与反转中不断升级。谁是正义谁又是邪恶？作品将对正义与人性的拷问推到了极致。"},
    {"subject_id": 35, "name": "ハチミツとクローバー", "name_cn": "蜂蜜与四叶草", "score": 8.3, "rank": 250, "rating_total": 8000, "date": "2005-04-14", "year": 2005, "platform": "TV", "eps": 24, "tags": [{"name":"青春","count":3000},{"name":"恋爱","count":2800},{"name":"J.C.STAFF","count":2500},{"name":"治愈","count":2200},{"name":"大学生","count":1500}], "nsfw": False, "chunk_text": "美术大学的学生们在青春年华中寻找自我、爱情和人生方向的故事。竹本、真山、森田、山田和花本五人之间细腻而复杂的情感纠葛贯穿了整个故事。作品以温柔的笔触描绘了青年们在即将踏入社会前那段迷茫而美好的时光，一首ハチミツ的插入曲让无数人泪流满面。"},
    # ── 2010年代经典 ──
    {"subject_id": 40, "name": "PSYCHO-PASS サイコパス", "name_cn": "心理测量者", "score": 8.0, "rank": 450, "rating_total": 15000, "date": "2012-10-11", "year": 2012, "platform": "TV", "eps": 22, "tags": [{"name":"科幻","count":3500},{"name":"赛博朋克","count":3000},{"name":"虚渊玄","count":2800},{"name":"公安","count":2500},{"name":"Production I.G","count":2000}], "nsfw": False, "chunk_text": "在近未来的日本，西比拉系统通过扫描人类的心理状态来决定每个人的职业、生活甚至存在价值。新人监视官常守朱被分配到公安局刑事课一系，结识了被标记为潜在犯的执行官狡噛慎也。当面对系统无法判断的犯罪者槙岛圣护时，正义的界限开始模糊。虚渊玄的剧本提出了一个根本性的问题——谁来监视监视者？"},
    {"subject_id": 41, "name": "四月は君の嘘", "name_cn": "四月是你的谎言", "score": 8.1, "rank": 400, "rating_total": 20000, "date": "2014-10-09", "year": 2014, "platform": "TV", "eps": 22, "tags": [{"name":"音乐","count":3800},{"name":"恋爱","count":3500},{"name":"催泪","count":3000},{"name":"A-1 Pictures","count":2500},{"name":"青春","count":2000}], "nsfw": False, "chunk_text": "天才钢琴少年有马公生在母亲去世后失去了倾听琴声的能力。14岁的春天，小提琴手宫园薰以自由奔放的演奏闯入了他的灰色世界。薰拉着公生重新站上舞台，让他一步步找回了音乐的色彩。然而薰身上隐藏的秘密将这段青春物语引向了最美丽的告别。四月来了，没有你的春天就要来了。"},
    {"subject_id": 42, "name": "ワンパンマン", "name_cn": "一拳超人", "score": 8.0, "rank": 500, "rating_total": 32000, "date": "2015-10-04", "year": 2015, "platform": "TV", "eps": 12, "tags": [{"name":"战斗","count":4500},{"name":"搞笑","count":3800},{"name":"MADHOUSE","count":3000},{"name":"英雄","count":2800},{"name":"漫画改","count":2500}], "nsfw": False, "chunk_text": "兴趣使然的英雄琦玉老师通过三年锻炼（每天100个俯卧撑、100个仰卧起坐、100个深蹲、10公里跑步）获得了无敌的力量——代价是失去了头发。任何敌人都只需一拳解决，这让琦玉陷入了找不到对手的倦怠之中。魔鬼改造人杰诺斯拜他为师，两人在英雄协会中展开了一系列啼笑皆非的战斗。"},
    {"subject_id": 43, "name": "３月のライオン", "name_cn": "3月的狮子", "score": 8.9, "rank": 22, "rating_total": 12000, "date": "2016-10-08", "year": 2016, "platform": "TV", "eps": 22, "tags": [{"name":"治愈","count":3500},{"name":"将棋","count":3000},{"name":"SHAFT","count":2500},{"name":"日常","count":2200},{"name":"新房昭之","count":2000}], "nsfw": False, "chunk_text": "17岁的职业将棋棋士桐山零在父母去世后独自生活。被三姐妹——川本家的明里、日向和小桃收养后，零在围棋盘的胜负世界外找到了家的温暖。作品以将棋为载体，细腻地描绘了孤独、成长与人与人之间的羁绊。新房昭之+羽海野千花的黄金组合将原作漫画的温暖质感完美再现。"},
    {"subject_id": 44, "name": "SHIROBAKO", "name_cn": "白箱", "score": 8.5, "rank": 100, "rating_total": 10000, "date": "2014-10-09", "year": 2014, "platform": "TV", "eps": 24, "tags": [{"name":"业界","count":3000},{"name":"P.A.WORKS","count":2800},{"name":"励志","count":2500},{"name":"日常","count":2200},{"name":"元动画","count":2000}], "nsfw": False, "chunk_text": "五位高中动画同好会的少女毕业后各自进入了动画行业的不同岗位。宫森葵成为了制作进行，每天在工期、预算和同事之间周旋。故事以动画制作现场为舞台，真实还原了一部TV动画从企划到播出的完整过程。无论是业内还是观众都将这部作品视为动画制作的教科书级作品。"},
    {"subject_id": 45, "name": "キルラキル", "name_cn": "斩服少女", "score": 8.0, "rank": 500, "rating_total": 12000, "date": "2013-10-03", "year": 2013, "platform": "TV", "eps": 24, "tags": [{"name":"热血","count":3000},{"name":"战斗","count":2800},{"name":"TRIGGER","count":2500},{"name":"原创","count":2200},{"name":"校园","count":1800}], "nsfw": False, "chunk_text": "缠流子为了调查父亲被杀之谜转学至本能字学园——一所由学生会会长鬼龙院罗晓以极制服统治的学校。流子穿着一件会说话的活体战斗水手服——鲜血——与学生会四天王展开了一系列惨烈又热血的对决。TRIGGER的出道作，以极具冲击力的视觉风格和胡闹般的节奏创造了一种无法被定义的体验。"},
    {"subject_id": 46, "name": "ソードアート・オンライン", "name_cn": "刀剑神域", "score": 6.5, "rank": 3000, "rating_total": 45000, "date": "2012-07-07", "year": 2012, "platform": "TV", "eps": 25, "tags": [{"name":"VR","count":4500},{"name":"网游","count":4000},{"name":"后宫","count":3000},{"name":"A-1 Pictures","count":2500},{"name":"轻小说改","count":2000}], "nsfw": False, "chunk_text": "2022年，完全沉浸式VRMMORPG刀剑神域正式上线。玩家桐人在进入游戏后发现无法登出——游戏设计者茅场晶彦宣布，通关前死亡则现实中也会死亡。桐人开始了在死亡游戏中的求生之旅，遇到了被称为闪光的细剑使亚丝娜。尽管口碑两极分化，SAO毫无疑问重新定义了异世界网游题材。"},
    {"subject_id": 47, "name": "ノーゲーム・ノーライフ", "name_cn": "NO GAME NO LIFE 游戏人生", "score": 7.5, "rank": 1100, "rating_total": 20000, "date": "2014-04-09", "year": 2014, "platform": "TV", "eps": 12, "tags": [{"name":"智斗","count":3500},{"name":"奇幻","count":3000},{"name":"轻小说改","count":2800},{"name":"兄妹","count":2200},{"name":"游戏","count":2000}], "nsfw": False, "chunk_text": "在现实世界中是无敌的游戏玩家的兄妹——空与白，被游戏之神特图召唤到了一个一切争端都以游戏解决的异世界。二人以空白这个名字挑战兽人种、天翼种等异种族，目标是将人类种从最弱种族带到统一世界的顶点。极致的画风和密集的智斗使其成为宅圈经典，可惜没有第二季。"},
    {"subject_id": 48, "name": "Re:ゼロから始める異世界生活", "name_cn": "Re:从零开始的异世界生活", "score": 7.5, "rank": 1100, "rating_total": 28000, "date": "2016-04-03", "year": 2016, "platform": "TV", "eps": 25, "tags": [{"name":"异世界","count":4500},{"name":"轮回","count":4000},{"name":"轻小说改","count":3500},{"name":"白狐","count":2000},{"name":"致郁","count":1800}], "nsfw": False, "chunk_text": "普通高中生菜月昴突然被召唤到一个奇幻异世界，并发现自己拥有死亡回归的能力——每次死后会回溯到某个存档点。昴决定用这个能力帮助银发半精灵艾米莉亚争夺王位。然而每一次死亡都伴随着精神的崩溃，第15话的绝望和第18话的告白成为了异世界题材的天花板。"},
    # ── 2020年代日常/喜剧 ──
    {"subject_id": 50, "name": "ゆるキャン△", "name_cn": "摇曳露营△", "score": 8.3, "rank": 250, "rating_total": 12000, "date": "2018-01-04", "year": 2018, "platform": "TV", "eps": 12, "tags": [{"name":"治愈","count":4000},{"name":"露营","count":3500},{"name":"日常","count":3000},{"name":"C-Station","count":2000},{"name":"美食","count":1800}], "nsfw": False, "chunk_text": "喜爱独自露营的高中生志摩凛在富士山脚下遇到了迷路的各务原抚子。看似孤僻的凛和活泼的抚子在篝火旁分享了一碗杯面，从此两人的露营故事开始。作品以极致放松的节奏和美丽的背景美术描绘了日本各地的露营胜地。看完只想立刻去露营——这大概是对露营番最高的赞美。"},
    {"subject_id": 51, "name": "かぐや様は告らせたい", "name_cn": "辉夜大小姐想让我告白", "score": 8.1, "rank": 400, "rating_total": 22000, "date": "2019-01-12", "year": 2019, "platform": "TV", "eps": 12, "tags": [{"name":"恋爱","count":4000},{"name":"喜剧","count":3800},{"name":"A-1 Pictures","count":3000},{"name":"学生会","count":2500},{"name":"漫画改","count":2200}], "nsfw": False, "chunk_text": "秀知院学园学生会的会长白银御行和副会长四宫辉夜都是天才中的天才。两人互相喜欢却因为超强的自尊心都不肯先告白——先告白的一方就输了！于是两人在日常中展开了各种心理战术，拼命要让对方先说出喜欢。第三季的奉心祭告白将这部恋爱喜剧推向了浪漫的极致。"},
    {"subject_id": 52, "name": "その着せ替え人形は恋をする", "name_cn": "更衣人偶坠入爱河", "score": 7.5, "rank": 1000, "rating_total": 15000, "date": "2022-01-08", "year": 2022, "platform": "TV", "eps": 12, "tags": [{"name":"恋爱","count":3500},{"name":"COSPLAY","count":3200},{"name":"CloverWorks","count":2800},{"name":"日常","count":2200},{"name":"漫画改","count":1800}], "nsfw": False, "chunk_text": "喜欢制作雏人偶的高中生五条新菜因为兴趣过于传统而一直独来独往。某天，班级的人气女生喜多川海梦发现了他的裁缝天赋，请他帮忙制作Cosplay服装。两人一个负责裁缝一个负责Cosplay，在共同创作的过程中逐渐走近。高素质的作画和糖度极高的互动让这部作品成为了年度恋爱番。", "collection": {"1": 5000, "2": 8000, "3": 1500}},
]

# Additional entries to reach ~100
_more = [
    {"subject_id": 100, "name": "カウボーイビバップ 天国の扉", "name_cn": "星际牛仔 天国之扉", "score": 8.5, "rank": 80, "rating_total": 8000, "date": "2001-09-01", "year": 2001, "platform": "Movie", "eps": 1, "tags": [{"name":"科幻","count":2000},{"name":"剧场版","count":1500}], "nsfw": False, "chunk_text": "SPIKE和JET追查一起生化恐怖袭击事件。被称为神之动画电影的存在，菅野洋子的配乐将西部片与黑色电影的风格融入科幻世界观。"},
    {"subject_id": 101, "name": "AKIRA", "name_cn": "阿基拉", "score": 8.2, "rank": 300, "rating_total": 10000, "date": "1988-07-16", "year": 1988, "platform": "Movie", "eps": 1, "tags": [{"name":"科幻","count":3000},{"name":"赛博朋克","count":2500}], "nsfw": False, "chunk_text": "近未来的东京，暴走族少年铁雄觉醒了超能力，一场关乎城市存亡的毁灭与新生开始了。大友克洋的这部作品重新定义了日本动画的技术上限与叙事深度。"},
    {"subject_id": 102, "name": "千と千尋の神隠し", "name_cn": "千与千寻", "score": 8.5, "rank": 50, "rating_total": 30000, "date": "2001-07-20", "year": 2001, "platform": "Movie", "eps": 1, "tags": [{"name":"奇幻","count":5000},{"name":"吉卜力","count":4500}], "nsfw": False, "chunk_text": "少女千寻误入神明世界，为了拯救变成猪的父母，她在汤婆婆的汤屋中打工。奥斯卡最佳动画长片，宫崎骏的想象力和对人性的理解达到了巅峰。"},
    {"subject_id": 103, "name": "もののけ姫", "name_cn": "幽灵公主", "score": 8.5, "rank": 40, "rating_total": 25000, "date": "1997-07-12", "year": 1997, "platform": "Movie", "eps": 1, "tags": [{"name":"奇幻","count":4000},{"name":"吉卜力","count":3800}], "nsfw": False, "chunk_text": "被诅咒的少年阿席达卡来到了铁镇，卷入了人类与森林神明之间的战争。宫崎骏对人类与自然关系的终极思考。"},
    {"subject_id": 104, "name": "風の谷のナウシカ", "name_cn": "风之谷", "score": 8.5, "rank": 30, "rating_total": 20000, "date": "1984-03-11", "year": 1984, "platform": "Movie", "eps": 1, "tags": [{"name":"奇幻","count":3500},{"name":"吉卜力","count":3000}], "nsfw": False, "chunk_text": "在腐海蔓延的末日世界中，风之谷的公主娜乌西卡探索着自然与人类的共生之路。宫崎骏早期建立起环保主义世界观的开山之作。"},
    {"subject_id": 105, "name": "天空の城ラピュタ", "name_cn": "天空之城", "score": 8.3, "rank": 200, "rating_total": 20000, "date": "1986-08-02", "year": 1986, "platform": "Movie", "eps": 1, "tags": [{"name":"冒险","count":3500},{"name":"吉卜力","count":3000}], "nsfw": False, "chunk_text": "少年巴鲁和少女希达寻找传说中漂浮在空中的天空之城拉普达。宫崎骏早期最纯粹的冒险故事，空中追逐的场面至今无人超越。"},
    {"subject_id": 106, "name": "となりのトトロ", "name_cn": "龙猫", "score": 8.3, "rank": 200, "rating_total": 25000, "date": "1988-04-16", "year": 1988, "platform": "Movie", "eps": 1, "tags": [{"name":"治愈","count":4500},{"name":"吉卜力","count":4000}], "nsfw": False, "chunk_text": "两姐妹在乡间遇到了森林的守护精灵龙猫。最治愈的吉卜力作品，龙猫成为了日本文化的象征。"},
    {"subject_id": 107, "name": "はたらく細胞", "name_cn": "工作细胞", "score": 7.0, "rank": 2000, "rating_total": 15000, "date": "2018-07-07", "year": 2018, "platform": "TV", "eps": 13, "tags": [{"name":"科普","count":3000},{"name":"喜剧","count":2500}], "nsfw": False, "chunk_text": "将人体内的细胞拟人化，讲述了红血球、白血球、血小板们在人体这个巨大世界中每天24小时无休地工作的故事。寓教于乐的科普佳作。"},
    {"subject_id": 108, "name": "ヱヴァンゲリヲン新劇場版:序", "name_cn": "EVA新剧场版:序", "score": 8.0, "rank": 500, "rating_total": 12000, "date": "2007-09-01", "year": 2007, "platform": "Movie", "eps": 1, "tags": [{"name":"科幻","count":2500},{"name":"机甲","count":2000}], "nsfw": False, "chunk_text": "新世纪福音战士的重启系列第一部，以全新的制作水准重新演绎了TV版前6话的故事。"},
    {"subject_id": 109, "name": "ヱヴァンゲリヲン新劇場版:破", "name_cn": "EVA新剧场版:破", "score": 8.5, "rank": 60, "rating_total": 15000, "date": "2009-06-27", "year": 2009, "platform": "Movie", "eps": 1, "tags": [{"name":"科幻","count":3000},{"name":"机甲","count":2500}], "nsfw": False, "chunk_text": "重启系列第二部，新角色真希波的加入和最后的觉醒为旧版粉丝带来了全新的体验，剧情开始大幅偏离TV版。"},
    {"subject_id": 110, "name": "シン・エヴァンゲリオン劇場版", "name_cn": "EVA新剧场版:终", "score": 8.5, "rank": 55, "rating_total": 18000, "date": "2021-03-08", "year": 2021, "platform": "Movie", "eps": 1, "tags": [{"name":"科幻","count":3500},{"name":"机甲","count":3000}], "nsfw": False, "chunk_text": "26年EVA系列的最终章。碇真嗣终于做出了自己的选择。再见了，所有的EVANGELION。"},
    {"subject_id": 111, "name": "サマーウォーズ", "name_cn": "夏日大作战", "score": 8.0, "rank": 500, "rating_total": 12000, "date": "2009-08-01", "year": 2009, "platform": "Movie", "eps": 1, "tags": [{"name":"科幻","count":2500},{"name":"家族","count":2000}], "nsfw": False, "chunk_text": "数学天才健二在假扮学姐夏希的男友参加家族聚会时，无意间引发了一场足以毁灭世界的网络危机。细田守的家族赞歌。"},
    {"subject_id": 112, "name": "バケモノの子", "name_cn": "怪物之子", "score": 7.5, "rank": 1100, "rating_total": 10000, "date": "2015-07-11", "year": 2015, "platform": "Movie", "eps": 1, "tags": [{"name":"奇幻","count":2500},{"name":"细田守","count":2000}], "nsfw": False, "chunk_text": "孤独的人类少年九太被怪物世界的剑士熊铁收养，在修炼中寻找自我。细田守对父子关系的深情诠释。"},
    {"subject_id": 113, "name": "パプリカ", "name_cn": "红辣椒", "score": 8.2, "rank": 350, "rating_total": 10000, "date": "2006-11-25", "year": 2006, "platform": "Movie", "eps": 1, "tags": [{"name":"科幻","count":3000},{"name":"今敏","count":2500}], "nsfw": False, "chunk_text": "通过进入他人梦境的装置DC Mini被盗，梦与现实开始交织。今敏的遗作，盗梦空间的灵感来源之一。"},
    {"subject_id": 114, "name": "PERFECT BLUE", "name_cn": "未麻的部屋", "score": 8.5, "rank": 35, "rating_total": 8000, "date": "1997-08-05", "year": 1997, "platform": "Movie", "eps": 1, "tags": [{"name":"悬疑","count":2500},{"name":"今敏","count":2000}], "nsfw": False, "chunk_text": "偶像转型演员的未麻在现实与幻想的边界逐渐迷失。今敏的处女作，黑色心理惊悚的里程碑，影响了包括黑天鹅在内的多部真人电影。"},
    {"subject_id": 115, "name": "千年女優", "name_cn": "千年女优", "score": 8.7, "rank": 18, "rating_total": 10000, "date": "2001-09-14", "year": 2001, "platform": "Movie", "eps": 1, "tags": [{"name":"今敏","count":2500}], "nsfw": False, "chunk_text": "隐退的女演员在30年后回忆自己一生追寻一个男人的故事。动画与现实的无缝切换创造了一种前所未有的叙事体验，今敏的巅峰之作。"},
    {"subject_id": 116, "name": "東京ゴッドファーザーズ", "name_cn": "东京教父", "score": 8.2, "rank": 300, "rating_total": 7000, "date": "2003-12-29", "year": 2003, "platform": "Movie", "eps": 1, "tags": [{"name":"喜剧","count":2000},{"name":"今敏","count":1800}], "nsfw": False, "chunk_text": "三个流浪汉在圣诞夜捡到一个弃婴，开始了一场为婴儿寻找父母的温暖之旅。今敏最温暖的作品。"},
    {"subject_id": 117, "name": "君の名は。", "name_cn": "你的名字。", "score": 8.0, "rank": 500, "rating_total": 50000, "date": "2016-08-26", "year": 2016, "platform": "Movie", "eps": 1, "tags": [{"name":"恋爱","count":5000},{"name":"新海诚","count":4500}], "nsfw": False, "chunk_text": "东京的男高中生立花泷和乡下的女高中生宫水三叶在梦中交换了身体。当他们开始寻找彼此时，发现横亘在两人之间的不仅仅是距离——还有时间。新海诚的现象级作品。"},
    {"subject_id": 118, "name": "秒速５センチメートル", "name_cn": "秒速5厘米", "score": 8.0, "rank": 450, "rating_total": 25000, "date": "2007-03-03", "year": 2007, "platform": "Movie", "eps": 1, "tags": [{"name":"恋爱","count":4000},{"name":"新海诚","count":3500}], "nsfw": False, "chunk_text": "远野贵树和篠原明里在小学时相爱，却因搬家而分开。三段短片讲述了一段无疾而终的初恋，秒速5厘米是樱花飘落的速度。新海诚的催泪成名作。"},
    {"subject_id": 119, "name": "言の葉の庭", "name_cn": "言叶之庭", "score": 7.8, "rank": 700, "rating_total": 15000, "date": "2013-05-31", "year": 2013, "platform": "Movie", "eps": 1, "tags": [{"name":"恋爱","count":3000},{"name":"新海诚","count":2800}], "nsfw": False, "chunk_text": "高中生秋月孝雄在雨天的日本庭园中遇到了神秘女子雪野。两人在雨中的邂逅逐渐发展为一段跨越年龄差距的细腻情感。雨景的作画达到了新海诚美学的新高度。"},
    {"subject_id": 120, "name": "天気の子", "name_cn": "天气之子", "score": 7.5, "rank": 1000, "rating_total": 35000, "date": "2019-07-19", "year": 2019, "platform": "Movie", "eps": 1, "tags": [{"name":"恋爱","count":4000},{"name":"新海诚","count":3500}], "nsfw": False, "chunk_text": "离家出走的少年帆高在东京遇到了能通过祈祷让天空放晴的少女阳菜。两人合作开展天气定制服务，却不知每一次晴天都在消耗着阳菜的生命。"},
    {"subject_id": 121, "name": "すずめの戸締まり", "name_cn": "铃芽之旅", "score": 7.8, "rank": 700, "rating_total": 25000, "date": "2022-11-11", "year": 2022, "platform": "Movie", "eps": 1, "tags": [{"name":"冒险","count":3500},{"name":"新海诚","count":3000}], "nsfw": False, "chunk_text": "少女岩户铃芽在九州遇到了寻找废墟中门的青年宗像草太。两人踏上了关闭灾难之门的旅程，从九州到东京，铃芽最终直面了童年时311大地震的创伤。新海诚的灾害三部曲终章。"},
    {"subject_id": 122, "name": "プロメア", "name_cn": "普罗米亚", "score": 7.0, "rank": 2500, "rating_total": 5000, "date": "2019-05-24", "year": 2019, "platform": "Movie", "eps": 1, "tags": [{"name":"机甲","count":2000},{"name":"TRIGGER","count":1800}], "nsfw": False, "chunk_text": "拥有燃烧能力的变异人类燃烧者们被消防队追捕。新进消防员加洛遇到了燃烧者组织Mad Burnish的领袖里欧，两人之间的碰撞引爆了一场地热级的对决。TRIGGER的视觉盛宴。"},
]

def main():
    settings = get_settings()
    init_db()

    print(f"Embedding dimension: {settings.EMBEDDING_DIMENSION}")
    print(f"Zhipu API key configured: {bool(settings.ZHIPU_API_KEY)}")

    if not settings.ZHIPU_API_KEY:
        print("❌ ZHIPU_API_KEY not configured. Cannot embed.")
        return

    # ── Build all subjects data ──
    subjects = []
    all_entries = ANIME_DATA + _more
    for entry in all_entries:
        subjects.append({
            "subject_id": entry["subject_id"],
            "name": entry["name"],
            "name_cn": entry["name_cn"],
            "chunk_text": entry["chunk_text"],
            "score": entry["score"],
            "rank": entry.get("rank", 0),
            "rating_total": entry.get("rating_total", 0),
            "rating_count": entry.get("rating_count", [0] * 10),
            "collection": entry.get("collection", {}),
            "date": entry.get("date"),
            "year": entry.get("year"),
            "platform": entry.get("platform", ""),
            "eps": entry.get("eps", 0),
            "tags": entry.get("tags", []),
            "nsfw": entry.get("nsfw", False),
        })

    print(f"\nPrepared {len(subjects)} subject entries")

    # ── Ingest ──
    ingestor = RagEntityIngestor(
        engine=engine,
        zhipu_api_key=settings.ZHIPU_API_KEY,
    )

    print("Starting ingestion...")
    try:
        count = ingestor.ingest_subjects(subjects)
        print(f"✅ Successfully ingested {count} subjects into rag_entities")
    except Exception as e:
        print(f"❌ Ingestion failed: {e}")
        return

    # ── Test retrieval ──
    print("\n── Testing RAG retrieval ──")
    retriever = RagEntityRetriever(
        engine=engine,
        zhipu_api_key=settings.ZHIPU_API_KEY,
    )

    test_queries = [
        ("高分机战番", "subject"),
        ("催泪治愈的作品", "subject"),
        ("时间旅行科幻", "subject"),
        ("经典机甲动画", "subject"),
        ("吉卜力电影", "subject"),
        ("2023年的热门作品", "subject"),
    ]

    for query, etype in test_queries:
        try:
            results = retriever.hybrid_search(
                query=query,
                entity_type=etype,
                limit=5,
            )
            top = f"{results[0].name}({results[0].name_cn}) d={results[0].cosine_distance:.3f}" if results else "NO RESULTS"
            print(f"  '{query}' → top: {top} ({len(results)} hits)")
        except Exception as e:
            print(f"  '{query}' → ❌ ERROR: {e}")

    print("\n✅ RAG ingestion and retrieval test complete!")

if __name__ == "__main__":
    main()
