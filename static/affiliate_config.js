/**
 * 相席ナビ アフィリエイト＆プロモーション統合管理マスター設定
 * 
 * 案件の追加・URL差し替え・カテゴリ変更は、このファイルを編集するだけで
 * サイト全体のカード・モーダル・バナーへ全自動で反映されます。
 */

const AFFILIATE_MASTER = {
    // 商業サイトでの有効化フラグ（社内モード時は自動的に無効）
    enabled: typeof IS_STAFF !== 'undefined' ? !IS_STAFF : true,

    // カテゴリ定義
    categories: [
        { id: 'all', name: 'すべて', icon: '✨' },
        { id: 'beauty', name: 'メンズ脱毛・美肌', icon: '🪒', highlight: true },
        { id: 'night', name: 'タクシー・深夜ケア', icon: '🚕', highlight: true },
        { id: 'dating', name: '恋活・即日出会い', icon: '💘' },
        { id: 'grooming', name: '香水・身だしなみ', icon: '👔' }
    ],

    // 案件マスター一覧
    items: [
        // ==========================================
        // 🪒 1. メンズ脱毛・美肌・清潔感（最注力ジャンル）
        // ==========================================
        {
            id: 'mens_hair_removal',
            category: 'beauty',
            featured: true,
            badge: '🔥 至近距離の必須マナー',
            tag: '青ヒゲ・医療脱毛',
            name: '相席ラウンジで圧倒的にモテる！メンズ医療ヒゲ脱毛プログラム',
            shortTitle: 'メンズ医療ヒゲ脱毛特集',
            infeedTag: '青ヒゲ対策',
            infeedSub: '至近距離の必須マナー',
            infeedTitle: 'メンズ医療ヒゲ脱毛プログラム',
            infeedCta: '公式 ›',
            target: '至近距離の会話で青ヒゲ・肌荒れが気になり、第一印象を劇的に良くしたい男性',
            desc: '照明が落ち着いた相席ラウンジでは、女性は男性の「口元の清潔感」を真っ先に見ています。夕方〜深夜でも青ヒゲが目立たない肌を整えることで、LINE交換率と2軒目への誘いやすさが段違いに跳ね上がります。',
            points: [
                '夜の店内でも清潔感をキープできる医療レーザー脱毛',
                '月々1,000円台〜手軽に始められる初回カウンセリングプラン',
                '相席した20代女性からの好感度・清潔感評価が劇的向上'
            ],
            cta: '限定キャンペーン・公式を見る ›',
            bannerImg: 'https://www27.a8.net/svt/bgt?aid=261020931365&wid=001&eno=01&mid=s00000014286015006000&mc=1',
            trackingImg: 'https://www17.a8.net/0.gif?a8mat=4BEL1F+61B8KY+328C+2HCB1D',
            url: 'https://px.a8.net/svt/ejp?a8mat=4BEL1F+61B8KY+328C+2HCB1D'
        },
        {
            id: 'mens_skincare_clinic',
            category: 'beauty',
            featured: false,
            badge: '💎 好感度UP',
            tag: '美肌・毛穴スキンケア',
            name: '女性ウケ抜群の第一印象を作る！ORBIS Mr.（オルビス ミスター）スキンケア',
            shortTitle: 'ORBIS Mr. スキンケア特集',
            infeedTag: '美肌ケア',
            infeedSub: 'テカリ・毛穴を徹底対策',
            infeedTitle: 'ORBIS Mr. メンズスキンケア',
            infeedCta: '公式 ›',
            target: 'テカリ・乾燥・毛穴を防ぎ、清潔感ある大人のモテ肌を手に入れたい男性',
            desc: '相席ラウンジのダウンライト下でも上品に見える「清潔な素肌」を作る人気メンズスキンケア「ORBIS Mr.（オルビス ミスター）」。洗顔・化粧水の簡単ケアで、初対面の第一印象を劇的に引き上げます。',
            points: [
                'ベタつき・テカリを抑えて爽やかな清潔感を長時間キープ',
                '20代〜30代女性に選ばれる好印象メンズスキンケア',
                '手軽に試せる初回限定スターターセットあり'
            ],
            cta: '初回限定セット・公式を見る ›',
            bannerImg: 'https://www28.a8.net/svt/bgt?aid=261020931376&wid=001&eno=01&mid=s00000008657018014000&mc=1',
            trackingImg: 'https://www18.a8.net/0.gif?a8mat=4BEL1F+67V08I+1USQ+2Z90WX',
            url: 'https://px.a8.net/svt/ejp?a8mat=4BEL1F+67V08I+1USQ+2Z90WX'
        },

        // ==========================================
        // 🚕 2. タクシー配車・ナイトケア（最注力ジャンル）
        // ==========================================
        {
            id: 'taxi_dispatch',
            category: 'night',
            featured: true,
            badge: '🚕 初回クーポンあり',
            tag: '終電後・スマート配車',
            name: '終電を逃してもスマート帰宅！タクシー配車アプリ（初回クーポン付き）',
            shortTitle: 'タクシー配車アプリ特集',
            infeedTag: '深夜送迎',
            infeedSub: '初回限定クーポン付き',
            infeedTitle: 'タクシー即時配車アプリ',
            infeedCta: '呼ぶ ›',
            target: '終電後の帰宅や、相席した女性をスマートにタクシーで送りたい男性',
            desc: 'ラウンジが最も盛り上がる23時〜深夜帯。路上でタクシーを探して慌てるのはNG。アプリで事前に呼んでおけば、女性をスマートにエスコートして好感度を最大化できます。',
            points: [
                'アプリ決済で車内の支払い不要＆割り勘もスムーズ',
                '現在地から一番近くのタクシーが最短数分で到着',
                'インストール後すぐに使える初回乗車割引クーポン配布中'
            ],
            cta: '初回クーポンで今すぐ配車 ›',
            // ※ASP審査完了後、GOタクシーやUber等のリンクに差し替え可能
            url: 'https://aiseki-navi.com/#taxi-coupon'
        },
        {
            id: 'alcohol_care_supplement',
            category: 'night',
            featured: false,
            badge: '🌙 翌朝スッキリ',
            tag: '二日酔い・アルコール対策',
            name: '相席で楽しく飲んだ翌朝もスッキリ！人気アルコール対策サプリメント',
            shortTitle: '翌朝アルコール対策サプリ',
            infeedTag: '翌朝対策',
            infeedSub: '二日酔いを残さない習慣',
            infeedTitle: '飲む前の肝臓ケアサプリ特集',
            infeedCta: '公式 ›',
            target: '週末や平日前夜にたくさん飲んでも、翌朝のだるさを残さず仕事やデートに備えたい方',
            desc: '相席ラウンジで乾杯が続いても安心。翌朝に響かせないための人気ウコン・肝臓エキス系サプリメントを厳選。カバンに忍ばせておくだけで、夜の相席を心置きなく楽しめます。',
            points: [
                '乾杯前・就寝前の1包で翌朝の目覚めが劇的に軽くなる',
                '持ち運びに便利な個包装でポケット・バッグに常備可能',
                'お酒好きの20代〜30代に支持される高評価リカバリー成分配合'
            ],
            cta: '公式お試しセットを見る ›',
            url: 'https://aiseki-navi.com/#care-supplement'
        },

        // ==========================================
        // 💘 3. 恋活・マッチング・即日出会い系
        // ==========================================
        {
            id: 'dating_instant',
            category: 'dating',
            featured: true,
            badge: '👑 待ち時間ゼロ',
            tag: '即日恋活・マッチング',
            name: '満席の待ち時間を回避！今夜すぐ近くで出会える人気マッチングアプリ',
            shortTitle: '即日出会える恋活アプリ特集',
            infeedTag: '即日恋活',
            infeedSub: '満席時の待ち時間ゼロ',
            infeedTitle: '即日出会える人気マッチングアプリ',
            infeedCta: '探す ›',
            target: '相席ラウンジの入店待ちが長い時や、今夜中に確実に飲める相手を見つけたい男性',
            desc: '「金曜夜に相席ラウンジに行ったら満席で1時間待ち…」そんな時に最も頼れるのが「今日これから会える」機能付きマッチングアプリ。待ち時間にアポを組むことで週末の空振りをゼロにできます。',
            points: [
                'GPSで現在地周辺の「いま飲みたい相手」を検索可能',
                '相席ラウンジ前の二刀流で週末の出会い成功率が倍増',
                '女性会員数が多く20代〜30代前半の利用者が急増中'
            ],
            cta: '無料登録・今夜の相手を探す ›',
            bannerImg: 'https://www24.a8.net/svt/bgt?aid=261019920127&wid=001&eno=01&mid=s00000016987001075000&mc=1',
            trackingImg: 'https://www18.a8.net/0.gif?a8mat=4BEK9C+23M2LU+3N2M+6EER5',
            url: 'https://px.a8.net/svt/ejp?a8mat=4BEK9C+23M2LU+3N2M+6EER5'
        },

        // ==========================================
        // 👔 4. 香水・身だしなみ・ファッション系
        // ==========================================
        {
            id: 'mens_fragrance',
            category: 'grooming',
            featured: false,
            badge: '✨ すれ違いで魅了',
            tag: '高級香水・身だしなみ',
            name: '至近距離でふわりと香る！人気ブランド香水の月額定期便（カラリア等）',
            shortTitle: 'モテる大人の香水特集',
            infeedTag: 'モテ香水',
            infeedSub: '女性ウケ抜群の香り厳選',
            infeedTitle: '大人のモテ香水・月額定期便',
            infeedCta: '詳細 ›',
            target: 'きつい香水は苦手だが、女性が思わず近寄りたくなる自然な良い香りをまといたい男性',
            desc: '相席ラウンジのソファー席で隣に座った瞬間、女性の印象を最も強く左右するのが「香り」です。高級ハイブランド香水を月々手軽に使い切れるサイズで試せる人気サービスです。',
            points: [
                '女性ウケの良い爽やか系・ウッディ系の人気香水を厳選',
                'ポケットサイズの専用アトマイザーでラウンジ入店前にひと吹き',
                'ボトル買いせず毎月好きな香りを少量で楽しめる'
            ],
            cta: '人気香水ランキングを見る ›',
            url: 'https://aiseki-navi.com/#fragrance-subscription'
        }
    ],

    // ランキング一覧に自然に挟み込むインフィードカード配置設定
    infeedSlots: [
        {
            afterRank: 5,
            itemId: 'dating_instant',
            badgeText: 'PR',
            highlight: false
        },
        {
            afterRank: 10,
            itemId: 'mens_hair_removal',
            badgeText: 'PR',
            highlight: true
        },
        {
            afterRank: 18,
            itemId: 'taxi_dispatch',
            badgeText: 'PR',
            highlight: true
        },
        {
            afterRank: 25,
            itemId: 'mens_fragrance',
            badgeText: 'PR',
            highlight: false
        }
    ]
};
