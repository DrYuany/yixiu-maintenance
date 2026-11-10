/**
 * 产品品牌配置。
 *
 * 本仓库是小一修独立仓库，缺省品牌名即「小一修」。
 * `VITE_PRODUCT_NAME` 仅作临时覆盖用（例如本地对比大一修文案）。
 * 其余文案（风场名、副标题、欢迎语）沿用原产品设定，不在此区分。
 */

const rawName = import.meta.env.VITE_PRODUCT_NAME;

export const PRODUCT_NAME = (rawName && String(rawName).trim()) || "小一修";

/** 会话栏 / 顶栏副标题：风机检修排程 Agent。 */
export const PRODUCT_TAGLINE = "风机检修排程 Agent";

/** 顶栏风场名，两产品共用。 */
export const SITE_NAME = "东海一号风场";

/** 浏览器标签页标题，按品牌拼。 */
export const DOCUMENT_TITLE = `${PRODUCT_NAME} · 海上风电检修排程`;
