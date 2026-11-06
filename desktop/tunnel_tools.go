package main

// The clients a tunnel provider runs that do not come with this application, pinned like the rest of the
// runtime: a version, the archive's hash, and a download only when the operator chooses that provider.
// Neither ships in a release. cloudflared is 40 MB for one provider, and frpc is flagged by Defender as a
// hack tool, which is frp's trouble and must not become a release's.
//
// A program on PATH is never used for these two. It would run with the token and all public traffic, and
// PATH holds folders an agent can write, the same reasoning that keeps the agent's sandbox from taking
// whatever bwrap comes first. And the program itself is pinned as well as the archive: it is hashed at
// each start, so a swapped file is replaced rather than trusted, and raising a pin moves every installation.
//
// ngrok is not here on purpose. Its terms ask written consent before its client is handed to people with
// their own accounts, so nothing here fetches it: the operator installs it from ngrok (tunneld/ngrok.go).

const (
	cloudflaredVersion = "2026.10.7"
	frpVersion         = "0.72.0"
)

func cloudflaredRaw(arch, sha string, size int64) download {
	name := "cloudflared-linux-" + arch
	return download{
		name: "cloudflared", version: cloudflaredVersion, kind: "binary", only: []string{"cloudflared"}, exec: []string{"cloudflared"},
		url:    "https://github.com/cloudflare/cloudflared/releases/download/" + cloudflaredVersion + "/" + name,
		sha256: sha, size: size,
	}
}

func cloudflaredArchive(arch, sha string, size int64) download {
	return download{
		name: "cloudflared", version: cloudflaredVersion, kind: "tar.gz", only: []string{"cloudflared"}, exec: []string{"cloudflared"},
		url:    "https://github.com/cloudflare/cloudflared/releases/download/" + cloudflaredVersion + "/cloudflared-darwin-" + arch + ".tgz",
		sha256: sha, size: size,
	}
}

// cloudflaredDownloads: the hashes are the release's own, in its notes, except the Mac's. The value its notes
// give for a .tgz is the hash of the program inside, so those two were read from the archives themselves.
var cloudflaredDownloads = map[string]download{
	"linux/amd64":  cloudflaredRaw("amd64", "1f99fb518b83dec2ba8d870a7cc991d82a77e6196295a3d2754ba17ab9f91872", 40274445),
	"linux/arm64":  cloudflaredRaw("arm64", "3df43aedc9ff291d1735b5cb576cb97e0b157488796db2e32b28aecabb445775", 37667293),
	"darwin/amd64": cloudflaredArchive("amd64", "02f3edfd87684dd5b705607688e4eb4b323f6e777b81e7b3a85af556aa356848", 21834416),
	"darwin/arm64": cloudflaredArchive("arm64", "3586ccf397e98e5f437e60e7c307ca6ac463e110580503af8bb8e6f9ebf831bd", 19847161),
	"windows/amd64": {
		name: "cloudflared", version: cloudflaredVersion, kind: "binary", only: []string{"cloudflared.exe"}, exec: []string{"cloudflared.exe"},
		url:    "https://github.com/cloudflare/cloudflared/releases/download/" + cloudflaredVersion + "/cloudflared-windows-amd64.exe",
		sha256: "2493dbc168923bb3e59e5e7716f71deced92992eea9acd6e9d74f84ac7751343", size: 55390512,
	},
}

func frpArchive(platform, sha string, size int64) download {
	return download{
		name: "frpc", version: frpVersion, kind: "tar.gz", strip: 1, only: []string{"frpc"}, exec: []string{"frpc"},
		url:    "https://github.com/fatedier/frp/releases/download/v" + frpVersion + "/frp_" + frpVersion + "_" + platform + ".tar.gz",
		sha256: sha, size: size,
	}
}

// frpDownloads: the hashes are the release's frp_sha256_checksums.txt.
var frpDownloads = map[string]download{
	"linux/amd64":  frpArchive("linux_amd64", "daf719b32ca2778770bb92955e231a07414d754378b9af328aa2642862479804", 14564595),
	"linux/arm64":  frpArchive("linux_arm64", "02180b857480302fd934e9ddbd50fe2f73229f99062f88756abc6de9df5e95cf", 12925619),
	"darwin/amd64": frpArchive("darwin_amd64", "60439dc6bf794c4182a1a4cac147d642f839d55201607208979eb23aecd09be9", 14560907),
	"darwin/arm64": frpArchive("darwin_arm64", "6e8576c5b6b9914a86850f6ff4fb81c5a09e706f91f70f64d5f398558bdc9db8", 13217943),
	"windows/amd64": {
		name: "frpc", version: frpVersion, kind: "zip", strip: 1, only: []string{"frpc.exe"}, exec: []string{"frpc.exe"},
		url:    "https://github.com/fatedier/frp/releases/download/v" + frpVersion + "/frp_" + frpVersion + "_windows_amd64.zip",
		sha256: "5197c1b69564089cdc229fc1260a0e25db1e722b1b7db45fe1542c3c43edf9ae", size: 14472216,
	},
}

// clientHashes is each client program as it comes out of its download, for the check at each start.
var clientHashes = map[string]map[string]string{
	tunnelCloudflare: {
		"linux/amd64":   "1f99fb518b83dec2ba8d870a7cc991d82a77e6196295a3d2754ba17ab9f91872",
		"linux/arm64":   "3df43aedc9ff291d1735b5cb576cb97e0b157488796db2e32b28aecabb445775",
		"darwin/amd64":  "5c55e77da7012cebca19170357e196176c48c9647cc2ead404702fa125ddbd86",
		"darwin/arm64":  "2136431d5eb467bf447d2ee514b713e192719b143aec1d9e09bf7dc290d5bb12",
		"windows/amd64": "2493dbc168923bb3e59e5e7716f71deced92992eea9acd6e9d74f84ac7751343",
	},
	tunnelFrp: {
		"linux/amd64":   "522352244a4872beaa0ad6fa136c8cc8d2e993e72bcf49f9c8a1d9683770d80e",
		"linux/arm64":   "b92f35651f25c45a414542d72150474688b6320af03192febcb418e0b4cf2644",
		"darwin/amd64":  "64b7422a5a497191aa96b509eae3c6acedfd24ba163aebea25ddce444f2ea5f4",
		"darwin/arm64":  "92f72de7473ca7918f9381e4e467495f4047413f706ee4756582a90706563980",
		"windows/amd64": "b8580b916ecb2e27ecc348bc67bc8b93b0e65266f85fd5d703e2f2f69862b089",
	},
}
