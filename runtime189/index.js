/**
 * 天翼云盘（189）签到 —— 由 Python 主控调用，结果写入 TY_RESULT_FILE。
 *
 * 之所以单独留一个 Node 小工具：189 没有可用的 Cookie 接口，登录要 RSA 加密 + 设备指纹，
 * 社区里成熟的实现是 cloud189-sdk（wes-lin/Cloud189Checkin 用的就是它）。
 * 重写协议的成本远大于收益，所以这里直接复用它，Python 侧只负责调度与汇总。
 */
const fs = require("fs");

const RESULT_FILE = process.env.TY_RESULT_FILE || "";
const TIMEOUT_MS = Number(process.env.TY_TIMEOUT_MS || 120000);

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
 */
function finish(payload) {
  writeResult(payload);
  const code = payload.ok ? 0 : 1;
  process.exitCode = code;
  setTimeout(() => process.exit(code), 50);
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
    if (userName && password) list.push({ userName, password });
  }
  if (process.env.TY_USERNAME && process.env.TY_PASSWORD) {
    list.push({ userName: process.env.TY_USERNAME, password: process.env.TY_PASSWORD });
  }
  return list.filter((item) => item && item.userName && item.password);
}

function toGB(size) {
  return Number(size || 0) / 1024 / 1024 / 1024;
}

/** 把 SDK 抛出的错误翻译成用户能照做的提示。 */
function describeError(err) {
  const body = (err && err.response && err.response.body) || {};
  let message = body.msg || body.message || body.errorMsg || (err && err.message) || String(err);
  if (typeof message !== "string") message = JSON.stringify(message);

  if (/验证码|captcha/i.test(message)) {
    return `登录被要求图形验证码（${message}）：先在网页端登录一次并信任当前设备，隔几分钟再试`;
  }
  if (/session/i.test(message)) {
    return "登录未通过（Can not get session）：常见原因是触发了图形验证码或设备校验，先手动登录一次再跑";
  }
  if (/设备|device/i.test(message)) {
    return `触发设备校验（${message}）：请关闭设备锁或信任新设备后再试`;
  }
  if (/密码|password/i.test(message)) {
    return `账号或密码不正确（${message}）`;
  }
  return message;
}

async function runOne(CloudClient, account) {
  const before = await (async () => {
    const client = new CloudClient({ username: account.userName, password: account.password });
    const info = await client.getUserSizeInfo();
    return { client, info };
  })();

  const sign = await before.client.userSign();
  const bonus = sign && sign.isSign ? 0 : Number((sign && sign.netdiskBonus) || 0);

  const after = await before.client.getUserSizeInfo();
  const cloudBefore = (before.info.cloudCapacityInfo || {}).totalSize || 0;
  const cloudAfter = (after.cloudCapacityInfo || {}).totalSize || 0;
  const familyBefore = (before.info.familyCapacityInfo || {}).totalSize || 0;
  const familyAfter = (after.familyCapacityInfo || {}).totalSize || 0;

  const lines = [
    ["账号", maskAccount(account.userName)],
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
  try {
    ({ CloudClient } = require("cloud189-sdk"));
  } catch (err) {
    finish({ ok: false, error: "未安装 cloud189-sdk，请先 npm install --prefix runtime189" });
    return;
  }

  const accounts = collectAccounts();
  if (!accounts.length) {
    finish({ ok: false, error: "未配置 189 账号（TY_USERNAME / TY_PASSWORD）" });
    return;
  }

  const lines = [];
  for (const account of accounts) {
    try {
      const result = await Promise.race([
        runOne(CloudClient, account),
        new Promise((_, reject) => setTimeout(() => reject(new Error("189 请求超时")), TIMEOUT_MS)),
      ]);
      lines.push(...result);
    } catch (err) {
      const detail = describeError(err);
      lines.push(["账号", maskAccount(account.userName)]);
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
