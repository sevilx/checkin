/**
 * 天翼云盘（189）签到 —— 由 Python 主控调用，结果写入 TY_RESULT_FILE。
 *
 * 为什么还要一个 Node 小工具：189 没有稳定的 Cookie 接口，登录要 RSA 加密 + 设备指纹，
 * 社区里成熟实现是 cloud189-sdk（wes-lin/Cloud189Checkin 用的也是它）。重写协议成本远大于收益。
 *
 * 这里补了 SDK 本身缺的两件事：
 * 1. sess 缓存（FileTokenStore）：cloud189-sdk 默认每次都用 MemoryStore，也就是每跑一次就
 *    登录一次。189 对"频繁登录"很敏感，天天登录极容易触发设备校验 / 图形验证码，最后只剩
 *    一句 "Can not get session."。把 token 落到磁盘后，一次登录能用 6 天，风险立刻降下来。
 * 2. 真实错误。SDK 的 getSession() 会把每种登录方式的异常都 catch 掉只丢给 logger，最后统一抛
 *    "Can not get session."，真正的原因（"图形验证码错误" / 设备校验 / 密码错）就丢了。
 *    所以这里接管 logger，把日志里的 message 收起来，再翻译成能照做的提示。
 */
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

// token 文件里是 accessToken，等同于登录态：只允许 owner 读写
process.umask(0o077);

const RESULT_FILE = process.env.TY_RESULT_FILE || "";
const TOKEN_DIR = process.env.TY_TOKEN_DIR || "";
const TIMEOUT_MS = Number(process.env.TY_TIMEOUT_MS || 120000);

/**
 * SDK 内部把每种登录方式的异常都 catch 掉，只丢给 logger，最后统一抛
 * "Can not get session."。而 189 真正的拒绝原因（"图形验证码错误" 之类）就埋在这些日志里。
 * 所以接管 logger：把 message 收进数组，不再往 stdout 打印（debug 日志会 dump 整页 HTML）。
 */
const sdkLogs = [];

function hookLogger(logger) {
  if (!logger) return;
  try {
    logger.configure({ consoleOutput: false, fileOutput: false, isDebugEnabled: false });
  } catch (err) {
    /* 版本差异，忽略 */
  }
  ["error", "warn"].forEach((name) => {
    const orig = logger[name];
    if (typeof orig !== "function") return;
    logger[name] = function (msg) {
      try {
        const text = typeof msg === "string" ? msg : (msg && msg.message) || "";
        const clean = String(text).replace(/\s+/g, " ").trim();
        if (clean) sdkLogs.push(clean.slice(0, 200));
      } catch (err) {
        /* 忽略 */
      }
    };
  });
}

function writeResult(payload) {
  const text = JSON.stringify(payload);
  if (RESULT_FILE) {
    fs.writeFileSync(RESULT_FILE, text, "utf-8");
  } else {
    process.stdout.write(`\n__RESULT__${text}\n`);
  }
}

/**
 * 写完结果就主动退出：SDK 内部有重试和定时器，出错后事件循环可能迟迟不结束，
 * 会让 Python 侧的 subprocess 一直等着。这里显式收尾。
 * 留 200ms 是为了让 FileTokenStore 的异步写盘落完 —— token 丢了就要重新登录，
 * 而重新登录正是触发设备校验的源头。
 */
function finish(payload) {
  writeResult(payload);
  const code = payload.ok ? 0 : 1;
  process.exitCode = code;
  setTimeout(() => process.exit(code), 200);
}

function maskAccount(name) {
  const text = String(name || "");
  if (text.length <= 7) return text;
  return `${text.slice(0, 3)}***${text.slice(-4)}`;
}

function collectAccounts() {
  const list = [];
  const raw = (process.env.TY_ACCOUNTS || "").trim();
  if (raw) {
    try {
      const parsed = JSON.parse(raw);
      (Array.isArray(parsed) ? parsed : [parsed]).forEach((item) => list.push(item));
    } catch (err) {
      /* 交给下面的编号写法兜底 */
    }
  }
  for (let i = 1; i <= 20; i += 1) {
    const userName = process.env[`TY_USERNAME_${i}`];
    const password = process.env[`TY_PASSWORD_${i}`];
    const sson = process.env[`TY_SSON_${i}`];
    if ((userName && password) || sson) list.push({ userName, password, sson });
  }
  if ((process.env.TY_USERNAME && process.env.TY_PASSWORD) || process.env.TY_SSON) {
    list.push({
      userName: process.env.TY_USERNAME || "",
      password: process.env.TY_PASSWORD || "",
      sson: process.env.TY_SSON || "",
    });
  }
  return list.filter((item) => item && ((item.userName && item.password) || item.sson));
}

/** 每个账号一个 token 文件，用账号哈希命名，避免明文手机号落到磁盘上。 */
function tokenFileFor(account) {
  if (!TOKEN_DIR) return "";
  try {
    fs.mkdirSync(TOKEN_DIR, { recursive: true });
  } catch (err) {
    return "";
  }
  const seed = String(account.userName || account.sson || "").trim();
  const name = crypto.createHash("sha1").update(seed).digest("hex").slice(0, 16);
  return path.join(TOKEN_DIR, `${name}.json`);
}

function buildOptions(FileTokenStore, account, filePath) {
  const options = {};
  if (filePath && FileTokenStore) {
    try {
      options.token = new FileTokenStore(filePath);
    } catch (err) {
      /* token 文件坏了就当没有，走正常登录 */
    }
  }
  if (account.sson) options.ssonCookie = String(account.sson).trim();
  if (account.userName && account.password) {
    options.username = String(account.userName);
    options.password = String(account.password);
  }
  return options;
}

/** 看看缓存里是不是还有效，用来告诉用户"这轮没有重新登录"。 */
function cachedSession(options) {
  const store = options.token;
  if (!store || typeof store.get !== "function") return null;
  try {
    const data = store.get() || {};
    if (data.accessToken && Number(data.expiresIn || 0) > Date.now()) return data;
  } catch (err) {
    /* 忽略 */
  }
  return null;
}

function toGB(size) {
  return Number(size || 0) / 1024 / 1024 / 1024;
}

/** 把 SDK 抛出的错误翻译成用户能照做的提示。 */
function describeError(err, account) {
  const response = (err && err.response) || {};
  const body = response.body || {};
  let message = body.msg || body.message || body.errorMsg || (err && err.message) || String(err);
  if (typeof message === "object" && message !== null) {
    message = JSON.stringify(message);
  }
  message = String(message || "");

  // "Can not get session." 是 SDK 的兜底文案，不是 189 的原话 ——
  // 真正的拒绝原因在它吞掉的日志里，优先用那份
  if (/Can not get session/i.test(message)) {
    const real = [...new Set(sdkLogs.filter((item) => !/Can not get session/i.test(item)))]
      .slice(-2)
      .join("；");
    message = real || message;
  }

  if (/验证码|captcha/i.test(message)) {
    return [
      `189 要求图形验证码（原始返回：${message}）。`,
      "这是 189 针对「新 IP / 非常用设备」的风控，密码没错也没用 —— 验证码脚本替不了你输。三条路按推荐顺序：",
      "1) 改用 SSON 登录（最稳）：浏览器登录 cloud.189.cn → F12 → Application → Cookies → 复制 SSON 的值，填到 TY189_SSON；",
      "2) 先在浏览器手动登录一次并通过验证，再跑本程序 —— 成功一次后 token 会缓存 6 天，不用天天登录；",
      "3) 等 30 分钟到 2 小时再试，189 的验证码限制会自动解除；当天失败次数太多就先停一天。",
    ].join("\n");
  }
  if (/Can not get session/i.test(message)) {
    return [
      "登录没通过：cloud189-sdk 把所有登录方式（缓存 / 刷新 / SSO / 账号密码）都试了一遍，全被 189 拒了，",
      "而它把每次失败的真实原因吞掉了，只抛这一句。按概率从高到低排查：",
      "1) 设备校验：去 cloud.189.cn → 账号安全，关掉「登录保护 / 设备锁」，或用手机 App 登录一次信任当前设备；",
      "2) 密码被截断：密码含 # $ 空格时要用引号整段包住；",
      "3) 出口 IP 变了（NAS / 机房 IP）：189 会要求二次验证，换回常用网络；",
      "4) 账号本身异常：需要短信验证，先去网页端处理。",
      "成功登录一次后 token 会缓存 6 天，之后不再反复登录，触发风控的概率会低很多。",
    ].join("\n");
  }
  if (/设备|device/i.test(message)) {
    return `触发设备校验（${message}）：请在 189 账号安全里关闭设备锁 / 登录保护后再试`;
  }
  if (
    account &&
    account.sson &&
    !account.password &&
    /Response code 40[01]|Bad Request|Unauthorized/i.test(message)
  ) {
    return (
      `SSON 登录被拒（${message}）：填的应该只是 SSON 这一个值（形如 一串字符），` +
      "不要把整段 cookie 抄进来；也可能 SSON 已经过期，重新登录 cloud.189.cn 后再取一次"
    );
  }
  if (/密码|password/i.test(message)) {
    return `账号或密码不正确（${message}）`;
  }
  if (/ECONNRESET|ETIMEDOUT|timeout|socket hang up/i.test(message)) {
    return `连接 189 失败（${message}）：换个时间重试，或检查 NAS 的出口网络`;
  }
  return message || "未知错误";
}

async function runOne(CloudClient, FileTokenStore, account) {
  const filePath = tokenFileFor(account);
  let options = buildOptions(FileTokenStore, account, filePath);
  const cached = cachedSession(options);

  let client;
  try {
    client = new CloudClient(options);
  } catch (err) {
    // 老版本 SDK 不认 token 参数时退回纯账号密码，不要让整个签到挂掉
    options = buildOptions(null, account, "");
    client = new CloudClient(options);
  }

  const before = await client.getUserSizeInfo();
  const sign = await client.userSign();
  const bonus = sign && sign.isSign ? 0 : Number((sign && sign.netdiskBonus) || 0);

  const after = await client.getUserSizeInfo();
  const cloudBefore = (before.cloudCapacityInfo || {}).totalSize || 0;
  const cloudAfter = (after.cloudCapacityInfo || {}).totalSize || 0;
  const familyBefore = (before.familyCapacityInfo || {}).totalSize || 0;
  const familyAfter = (after.familyCapacityInfo || {}).totalSize || 0;

  const lines = [
    ["账号", maskAccount(account.userName || "SSO 登录")],
    ["登录", cached ? "复用上次的会话（没有重新登录）" : account.sson && !account.userName ? "SSO Cookie 登录" : "账号密码登录"],
    ["个人签到", bonus > 0 ? `获得 ${bonus}M 空间` : sign && sign.isSign ? "今日已签到" : "已完成"],
    [
      "个人容量",
      `+${((cloudAfter - cloudBefore) / 1024 / 1024).toFixed(2)}M / 共 ${toGB(cloudAfter).toFixed(2)}G`,
    ],
  ];
  if (familyAfter) {
    lines.push([
      "家庭容量",
      `+${((familyAfter - familyBefore) / 1024 / 1024).toFixed(2)}M / 共 ${toGB(familyAfter).toFixed(2)}G`,
    ]);
  }
  return lines;
}

async function main() {
  let CloudClient;
  let FileTokenStore;
  try {
    const sdk = require("cloud189-sdk");
    CloudClient = sdk.CloudClient;
    FileTokenStore = sdk.FileTokenStore;
    hookLogger(sdk.logger);
  } catch (err) {
    finish({ ok: false, error: "未安装 cloud189-sdk，请先 npm install --prefix runtime189" });
    return;
  }

  const accounts = collectAccounts();
  if (!accounts.length) {
    finish({ ok: false, error: "未配置 189 账号（TY189_USERNAME / TY189_PASSWORD，或 TY189_SSON）" });
    return;
  }

  const lines = [];
  for (const account of accounts) {
    try {
      const result = await Promise.race([
        runOne(CloudClient, FileTokenStore, account),
        new Promise((_, reject) => setTimeout(() => reject(new Error("189 请求超时")), TIMEOUT_MS)),
      ]);
      lines.push(...result);
    } catch (err) {
      const detail = describeError(err, account);
      lines.push(["账号", maskAccount(account.userName || "SSO 登录")]);
      lines.push(["签到失败", detail]);
      finish({ ok: false, lines, error: detail });
      return;
    }
  }
  finish({ ok: true, lines });
}

main().catch((err) => {
  finish({ ok: false, error: (err && err.message) || String(err) });
});
