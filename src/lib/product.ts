/**
 * 产品品牌配置。
 *
 * 一修与小一修共用同一套前端，唯一的差别是品牌名。
 * 构建小一修时设 `VITE_PRODUCT_NAME=小一修`；缺省是「一修」。
 * 其余文案（风场名、副标题、欢迎语）两产品一致，不在此区分。
 */

const rawName = import.meta.env.VITE_PRODUCT_NAME;

export const PRODUCT_NAME = (rawName && String(rawName).trim()) || "一修";

/** 会话栏 / 顶栏副标题：风机检修排程 Agent。 */
export const PRODUCT_TAGLINE = "风机检修排程 Agent";

/** 顶栏风场名，两产品共用。 */
export const SITE_NAME = "东海一号风场";

/** 浏览器标签页标题，按品牌拼。 */
export const DOCUMENT_TITLE = `${PRODUCT_NAME} · 海上风电检修排程`;
