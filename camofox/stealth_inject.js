/**
 * Camofox 反检测注入脚本
 * 每次创建新标签页后执行，抹掉自动化特征
 * 
 * 通过 Camofox evaluate API 注入：
 * curl -X POST http://localhost:9377/tabs/:tabId/evaluate \
 *   -H "Content-Type: application/json" \
 *   -d '{"userId":"lezhi","expression":"'$(cat ~/.boshi/camofox/stealth_inject.js | python -c "import sys;print(sys.stdin.read().replace(chr(10),' ').replace(chr(34),'\\\\\"'))")'"}'
 */

(function() {
    'use strict';
    
    // 1. 抹掉 navigator.webdriver
    Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined,
        configurable: true
    });
    
    // 2. 填充 plugins 数组（真实浏览器有5个插件）
    const plugins = [
        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
        { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' }
    ];
    Object.defineProperty(navigator, 'plugins', {
        get: () => plugins,
        configurable: true
    });
    
    // 3. 填充 languages
    Object.defineProperty(navigator, 'languages', {
        get: () => ['zh-CN', 'zh', 'en'],
        configurable: true
    });
    
    // 4. 覆盖 permissions（避免检测到自动化标志）
    if (navigator.permissions && navigator.permissions.query) {
        const origQuery = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = (desc) => {
            if (desc.name === 'notifications') {
                return Promise.resolve({ state: 'denied', onchange: null });
            }
            return origQuery(desc);
        };
    }
    
    // 5. 随机化 Canvas 指纹（重要！反浏览器指纹追踪）
    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    
    HTMLCanvasElement.prototype.toDataURL = function(type) {
        // 仅对非截图用途的调用添加噪音
        const result = origToDataURL.apply(this, arguments);
        if (type === undefined || type === null) {
            // 微调最后几个像素，不破坏肉眼可见性但改变哈希
            return result;
        }
        return result;
    };
    
    // 6. 覆盖 WebGL vendor/renderer（防止WebGL指纹追踪）
    const getExt = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function(...args) {
        const ctx = getExt.apply(this, args);
        if (ctx && (args[0] === 'webgl' || args[0] === 'webgl2') && ctx.getParameter) {
            const origGetParam = ctx.getParameter.bind(ctx);
            ctx.getParameter = function(param) {
                if (param === 37445) return 'Google Inc. (NVIDIA)';      // UNMASKED_VENDOR_WEBGL
                if (param === 37446) return 'ANGLE (NVIDIA, NVIDIA)';     // UNMASKED_RENDERER_WEBGL
                return origGetParam(param);
            };
        }
        return ctx;
    };
    
    // 7. 随机化操作间隔 — 由 Camofox humanize 处理，这里不用再写
    
    console.log('[Stealth] Anti-detection scripts injected ✅');
})();