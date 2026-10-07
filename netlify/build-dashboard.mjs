import { readFile, writeFile } from "node:fs/promises";

const templatePath = new URL("../dashboard/templates/optimizer_review.html", import.meta.url);
const outputPath = new URL("./site/index.html", import.meta.url);

const mainDashboardLink = '<a class="top-link" href="/">Main dashboard</a>';
const getHelper = "const get=async(url,timeoutMs=15000)=>{";
const fetchCall = 'fetch(url,{cache:"no-store"';

let html = await readFile(templatePath, "utf8");

if (!html.includes(mainDashboardLink)) {
  throw new Error("The optimizer dashboard Main dashboard link was not found");
}
if (!html.includes(getHelper) || !html.includes(fetchCall)) {
  throw new Error("The optimizer dashboard read helper could not be adapted for Netlify");
}

html = html
  .replace(mainDashboardLink, "")
  .replace(
    getHelper,
    'const readOnlyUrl=url=>url.startsWith("/api/")?`/.netlify/functions/live-data?view=${encodeURIComponent(url.slice(5))}`:url;\n    const get=async(url,timeoutMs=15000)=>{',
  )
  .replace(fetchCall, 'fetch(readOnlyUrl(url),{cache:"no-store"');

await writeFile(outputPath, html, "utf8");
