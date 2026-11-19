"use strict";
const nativeFetch=window.fetch.bind(window);
window.fetch=async(input,options)=>{const url=new URL(typeof input==='string'?input:input.url,location.href);if(url.origin===location.origin&&url.pathname.includes('/assets/')&&(!options?.method||options.method==='GET'))return nativeFetch(input,options);throw new Error("Read-only public source preview: no robot connection");};
window.addEventListener("DOMContentLoaded",()=>{
 const banner=document.createElement("div");banner.textContent="READ-ONLY SOURCE PREVIEW / 离线源码预览 · No robot connection";
 banner.style.cssText="position:fixed;top:0;left:0;right:0;z-index:99999;background:#eaf0f7;padding:7px 20px;text-align:center;font:14px system-ui;color:#172331";document.body.prepend(banner);
 document.body.style.paddingTop="36px";
 const style=document.createElement('style');style.textContent='#attention-center,#stop-latch-banner{display:none!important}';document.head.append(style);
 document.addEventListener("click",e=>{const button=e.target.closest("button");const a=e.target.closest("a");
 if(button&&!button.matches("[data-page],#show-details,#refresh-state,[data-attention-open],[data-attention-close]")){e.preventDefault();e.stopImmediatePropagation();}
 if(a&&a.getAttribute("href")&&!a.getAttribute("href").startsWith("#")&&!a.getAttribute("href").startsWith("./review/")&&!a.getAttribute("href").startsWith("../index.html")){e.preventDefault();e.stopImmediatePropagation();}},true);
});
