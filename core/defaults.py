DEFAULT_DETECTION_THRESHOLD = 10

DEFAULT_OCR_KEYWORDS = {
	"airdrop": {
		"points": 5,
		"aliases": [
			"air drop",
			"airdrops",
		],
	},
	"andrew tate": {
		"points": 2,
		"aliases": [
			"@cobratate",
			"andrewtate",
			"cobratate",
		],
	},
	"bitcoin": {
		"points": 4,
		"aliases": [
			"bit coin",
		],
	},
	"bonus": {
		"points": 3,
		"aliases": [],
	},
	"casino": {
		"points": 5,
		"aliases": [],
	},
	"crypto": {
		"points": 4,
		"aliases": [
			"cryptocurrency",
		],
	},
	"eth": {
		"points": 2,
		"aliases": [],
	},
	"ethereum": {
		"points": 4,
		"aliases": [],
	},
	"free money": {
		"points": 5,
		"aliases": [
			"freemoney",
		],
	},
	"giveaway": {
		"points": 3,
		"aliases": [
			"give away",
		],
	},
	"launch": {
		"points": 2,
		"aliases": [],
	},
	"mrbeast": {
		"points": 2,
		"aliases": [
			"@mrbeast",
			"mr beast",
		],
	},
	"promo code": {
		"points": 3,
		"aliases": [
			"promo",
			"promocode",
		],
	},
	"register": {
		"points": 2,
		"aliases": [],
	},
	"usdt": {
		"points": 4,
		"aliases": [
			"usd",
		],
	},
	"weakox.com": {
		"points": 5,
		"aliases": [
			"weakox",
			"weakox com",
			"weakoxcom",
		],
	},
	"withdraw": {
		"points": 3,
		"aliases": [
			"cash out",
			"withdrawal",
		],
	},
	"x.com": {
		"points": 2,
		"aliases": [
			"twitter",
			"x com",
			"xcom",
		],
	},
}

DEFAULT_PRESSURE_SETTINGS = {
	"enabled": 0,
	"log_channel_id": None,
	"threshold": 100,
	"decay_per_second": 3.3,
	"base_pressure": 10,
	"attachment_pressure": 15,
	"embed_pressure": 15,
	"mention_pressure": 10,
	"link_pressure": 15,
	"duplicate_pressure": 35,
	"new_member_pressure": 20,
	"line_pressure": 5,
	"solo_emote_pressure": 30,
	"gif_pressure": 50,
	"banned_word_pressure": 500,
	"new_member_hours": 24,
	"role_duration_seconds": 3600,
	"delete_message": 0,
	"give_role": 0,
}

DEFAULT_PRESSURE_BANNED_WORDS = []
