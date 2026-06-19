// Step 1: Click phone login tab
let iframe = document.querySelector("iframe");
let idoc = iframe.contentDocument;

let smsTab = [...idoc.querySelectorAll("a, span, div")].find(el => el.textContent.trim() === "手机号登录");
if (smsTab) {
    smsTab.click();
    "clicked phone tab";
} else {
    "no phone tab found";
}
