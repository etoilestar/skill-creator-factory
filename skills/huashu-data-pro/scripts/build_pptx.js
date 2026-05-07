#!/usr/bin/env node
/**
 * build_pptx.js — 将多个 HTML 幻灯片文件构建为一个 PPTX 文件
 *
 * 用法：
 *   node build_pptx.js --slides slide1.html slide2.html --output report.pptx
 *   node build_pptx.js --dir ./slides/ --output report.pptx
 *   node build_pptx.js --slides slide1.html --output report.pptx --chart 0:bar:data.json
 *   node build_pptx.js --stdin-json --output report.pptx   (从 stdin 读取 JSON 数组)
 *
 * 参数：
 *   --slides    指定 HTML 文件列表（按顺序）
 *   --dir       指定包含 HTML 文件的目录（按文件名排序）
 *   --stdin-json  从 stdin 读取 JSON 数组：[{"name":"slide1.html","html":"..."}]
 *   --output    输出 PPTX 文件名（默认：OUTPUT_DIR/output.pptx 或 output.pptx）
 *   --chart     在指定幻灯片的 placeholder 中插入图表
 *               格式：幻灯片序号:图表类型:数据JSON文件
 *               例：0:bar:chart_data.json
 *
 * 依赖安装（自动检测，缺失时提示）：
 *   npm install pptxgenjs playwright sharp
 *
 * HTML 文件规范：
 *   - body 尺寸必须是 width: 720pt; height: 405pt（16:9）
 *   - 所有文字必须在 <p>/<h1>-<h6>/<ul>/<ol> 标签内
 *   - 图表预留区域用 <div class="placeholder" id="chart-1"></div>
 *   - 不支持 CSS 渐变（需预渲染为 PNG）
 *   - 颜色使用 hex 格式
 */

const path = require('path');
const fs = require('fs');
const os = require('os');

// 检查依赖
function checkDependencies() {
  const missing = [];
  for (const dep of ['pptxgenjs', 'playwright', 'sharp']) {
    try {
      require.resolve(dep);
    } catch {
      missing.push(dep);
    }
  }
  if (missing.length > 0) {
    console.error(`缺少依赖: ${missing.join(', ')}`);
    console.error(`请运行: npm install ${missing.join(' ')}`);
    process.exit(1);
  }
}

checkDependencies();

const pptxgen = require('pptxgenjs');
const html2pptx = require('./html2pptx.js');

// 解析命令行参数
function parseArgs() {
  const args = process.argv.slice(2);
  // 当宿主注入了 OUTPUT_DIR 环境变量时，默认输出到该目录下；否则输出到当前目录
  const defaultOutput = process.env.OUTPUT_DIR
    ? path.join(process.env.OUTPUT_DIR, 'output.pptx')
    : 'output.pptx';
  const config = { slides: [], output: defaultOutput, charts: [], stdinJson: false };

  for (let i = 0; i < args.length; i++) {
    switch (args[i]) {
      case '--slides':
        while (i + 1 < args.length && !args[i + 1].startsWith('--')) {
          config.slides.push(args[++i]);
        }
        break;
      case '--dir':
        const dir = args[++i];
        if (!fs.existsSync(dir)) {
          console.error(`目录不存在: ${dir}`);
          process.exit(1);
        }
        config.slides = fs.readdirSync(dir)
          .filter(f => f.endsWith('.html'))
          .sort()
          .map(f => path.join(dir, f));
        break;
      case '--output':
        config.output = args[++i];
        break;
      case '--chart':
        // 格式: slideIndex:chartType:dataFile
        const parts = args[++i].split(':');
        config.charts.push({
          slideIndex: parseInt(parts[0]),
          chartType: parts[1],
          dataFile: parts[2]
        });
        break;
      case '--stdin-json':
        config.stdinJson = true;
        break;
      case '--help':
        console.log(`
用法：node build_pptx.js [选项]

选项：
  --slides file1.html file2.html   指定HTML幻灯片文件
  --dir ./slides/                  从目录加载所有HTML文件
  --stdin-json                     从stdin读取幻灯片JSON（格式：[{"name":"slide1.html","html":"..."}]）
  --output report.pptx             输出文件名（默认：OUTPUT_DIR/output.pptx 或 output.pptx）
  --chart 0:bar:data.json          插入图表到指定幻灯片
  --help                           显示帮助
        `);
        process.exit(0);
    }
  }

  if (config.slides.length === 0 && !config.stdinJson) {
    console.error('请指定至少一个HTML文件。使用 --help 查看帮助。');
    process.exit(1);
  }

  return config;
}

// 图表类型映射
function getChartType(pptx, typeName) {
  const map = {
    'bar': pptx.charts.BAR,
    'col': pptx.charts.BAR,
    'line': pptx.charts.LINE,
    'pie': pptx.charts.PIE,
    'scatter': pptx.charts.SCATTER,
    'doughnut': pptx.charts.DOUGHNUT
  };
  return map[typeName.toLowerCase()] || pptx.charts.BAR;
}

// 默认图表配色（不带 # 前缀！PptxGenJS 规则）
const CHART_COLORS = ['E17055', '45B7AA', '5B8C5A', 'FFD700', '9B7EDE'];

async function build() {
  const config = parseArgs();

  // --stdin-json：从 stdin 读取幻灯片 JSON 数组，写到临时目录
  let tmpDir = null;
  if (config.stdinJson) {
    let stdinText;
    try {
      stdinText = fs.readFileSync(0, 'utf-8');
    } catch (err) {
      console.error('读取 stdin 失败:', err.message);
      process.exit(1);
    }

    let slides;
    try {
      slides = JSON.parse(stdinText);
    } catch (err) {
      console.error('stdin JSON 解析失败:', err.message);
      process.exit(1);
    }

    if (!Array.isArray(slides) || slides.length === 0) {
      console.error('stdin JSON 必须是非空数组，格式：[{"name":"slide1.html","html":"..."}]');
      process.exit(1);
    }

    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pptx-slides-'));
    for (const slide of slides) {
      if (!slide.name || !slide.html) {
        console.error('每个幻灯片对象必须包含 name 和 html 字段');
        process.exit(1);
      }
      const filePath = path.join(tmpDir, slide.name);
      fs.writeFileSync(filePath, slide.html, 'utf-8');
      config.slides.push(filePath);
    }
  }

  try {
    await _build(config);
  } finally {
    // 清理临时目录
    if (tmpDir) {
      try {
        fs.rmSync(tmpDir, { recursive: true, force: true });
      } catch (_) {
        // 清理失败不影响结果
      }
    }
  }
}

async function _build(config) {

  console.log(`构建 PPTX: ${config.slides.length} 页幻灯片`);

  const pptx = new pptxgen();
  pptx.layout = 'LAYOUT_16x9';

  // 存储每页的 placeholders 以便后续插入图表
  const slideResults = [];

  for (let i = 0; i < config.slides.length; i++) {
    const htmlFile = config.slides[i];
    const absPath = path.isAbsolute(htmlFile) ? htmlFile : path.join(process.cwd(), htmlFile);

    if (!fs.existsSync(absPath)) {
      console.error(`文件不存在: ${absPath}`);
      process.exit(1);
    }

    console.log(`  [${i + 1}/${config.slides.length}] ${path.basename(htmlFile)}`);

    try {
      const result = await html2pptx(absPath, pptx);
      slideResults.push(result);
    } catch (error) {
      console.error(`  ❌ 转换失败: ${error.message}`);
      process.exit(1);
    }
  }

  // 插入图表
  for (const chartConfig of config.charts) {
    const { slideIndex, chartType, dataFile } = chartConfig;

    if (slideIndex >= slideResults.length) {
      console.error(`图表配置错误: 幻灯片 ${slideIndex} 不存在（共 ${slideResults.length} 页）`);
      continue;
    }

    const { slide, placeholders } = slideResults[slideIndex];
    if (placeholders.length === 0) {
      console.error(`幻灯片 ${slideIndex} 没有 placeholder 区域`);
      continue;
    }

    const dataPath = path.isAbsolute(dataFile) ? dataFile : path.join(process.cwd(), dataFile);
    if (!fs.existsSync(dataPath)) {
      console.error(`图表数据文件不存在: ${dataPath}`);
      continue;
    }

    const chartData = JSON.parse(fs.readFileSync(dataPath, 'utf-8'));
    const type = getChartType(pptx, chartType);

    // 使用第一个 placeholder 的位置
    const pos = placeholders[0];
    const chartOptions = {
      x: pos.x,
      y: pos.y,
      w: pos.w,
      h: pos.h,
      chartColors: chartData.colors || CHART_COLORS,
      showTitle: !!chartData.title,
      title: chartData.title || '',
      showCatAxisTitle: !!chartData.catAxisTitle,
      catAxisTitle: chartData.catAxisTitle || '',
      showValAxisTitle: !!chartData.valAxisTitle,
      valAxisTitle: chartData.valAxisTitle || ''
    };

    // 柱状图特有配置
    if (chartType === 'col') chartOptions.barDir = 'col';
    if (chartType === 'bar') chartOptions.barDir = 'bar';

    // 折线图特有配置
    if (chartType === 'line') {
      chartOptions.lineSize = 3;
      chartOptions.lineSmooth = true;
    }

    // 饼图特有配置
    if (chartType === 'pie' || chartType === 'doughnut') {
      chartOptions.showPercent = true;
      chartOptions.showLegend = true;
      chartOptions.legendPos = 'r';
    }

    slide.addChart(type, chartData.series, chartOptions);
    console.log(`  📊 已插入图表到幻灯片 ${slideIndex}`);
  }

  // 输出文件
  const outputPath = path.isAbsolute(config.output) ? config.output : path.join(process.cwd(), config.output);
  // 确保输出目录存在（例如 outputs/）
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  await pptx.writeFile({ fileName: outputPath });
  console.log(`\n✅ 已生成: ${outputPath}`);
}

build().catch(err => {
  console.error('构建失败:', err.message);
  process.exit(1);
});
