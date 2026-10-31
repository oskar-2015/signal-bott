/* خلفية شموع تداول متحركة ترسم بالكود (لا صور ولا أصوات).
   تتوقف عند إخفاء الصفحة، وتصبح ثابتة إذا كان الجهاز يفضّل تقليل الحركة. */
(function () {
  var canvas = document.getElementById('bg');
  if (!canvas || !canvas.getContext) return;
  var ctx = canvas.getContext('2d');
  var reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;

  var UP = '47,211,154', DOWN = '255,98,117', GOLD = '230,181,60';
  var W = 0, H = 0, dpr = 1;
  var candles = [], price = 100, trend = 0;
  var bodyW = 12, gap = 8, step = bodyW + gap;
  var offset = 0, speed = 14; // بكسل في الثانية
  var last = 0;

  function nextCandle() {
    trend += (Math.random() - 0.5) * 0.5;
    trend *= 0.92;
    var open = price;
    var move = trend * 2.2 + (Math.random() - 0.5) * 3.4;
    var close = open + move;
    var high = Math.max(open, close) + Math.random() * 2.2;
    var low = Math.min(open, close) - Math.random() * 2.2;
    price = close;
    return { o: open, c: close, h: high, l: low };
  }

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    W = window.innerWidth; H = window.innerHeight;
    canvas.width = Math.floor(W * dpr);
    canvas.height = Math.floor(H * dpr);
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';
    var small = W < 600;
    bodyW = small ? 9 : 13; gap = small ? 6 : 9; step = bodyW + gap;
    var need = Math.ceil(W / step) + 3;
    candles = [];
    price = 100; trend = 0;
    for (var i = 0; i < need; i++) candles.push(nextCandle());
    draw();
  }

  function draw() {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    // شبكة أفقية خفيفة
    ctx.strokeStyle = 'rgba(147,163,196,0.10)';
    ctx.lineWidth = 1;
    for (var g = 1; g < 7; g++) {
      var gy = Math.round(H * g / 7) + 0.5;
      ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(W, gy); ctx.stroke();
    }

    var min = Infinity, max = -Infinity, i, c;
    for (i = 0; i < candles.length; i++) {
      c = candles[i];
      if (c.l < min) min = c.l;
      if (c.h > max) max = c.h;
    }
    var pad = (max - min) * 0.12 || 1;
    min -= pad; max += pad;
    var top = H * 0.12, bottom = H * 0.88;
    function y(v) { return bottom - (v - min) / (max - min) * (bottom - top); }

    // الشموع
    for (i = 0; i < candles.length; i++) {
      c = candles[i];
      var x = i * step - offset;
      var upc = c.c >= c.o;
      var col = upc ? UP : DOWN;
      ctx.strokeStyle = 'rgba(' + col + ',0.62)';
      ctx.fillStyle = 'rgba(' + col + ',0.40)';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(x + bodyW / 2, y(c.h));
      ctx.lineTo(x + bodyW / 2, y(c.l));
      ctx.stroke();
      var yo = y(c.o), yc = y(c.c);
      var bt = Math.min(yo, yc), bh = Math.max(2, Math.abs(yo - yc));
      ctx.fillRect(x, bt, bodyW, bh);
      ctx.strokeRect(x, bt, bodyW, bh);
    }

    // خط المتوسط الذهبي
    ctx.strokeStyle = 'rgba(' + GOLD + ',0.6)';
    ctx.lineWidth = 2;
    ctx.beginPath();
    var n = 6, started = false;
    for (i = 0; i < candles.length; i++) {
      var s = 0, k = 0;
      for (var j = Math.max(0, i - n + 1); j <= i; j++) { s += candles[j].c; k++; }
      var px = i * step - offset + bodyW / 2, py = y(s / k);
      if (!started) { ctx.moveTo(px, py); started = true; } else ctx.lineTo(px, py);
    }
    ctx.stroke();
  }

  function frame(t) {
    if (document.hidden) { last = 0; requestAnimationFrame(frame); return; }
    if (!last) last = t;
    var dt = Math.min(t - last, 100);
    if (dt >= 33) { // حوالي 30 إطاراً في الثانية لتوفير البطارية
      last = t;
      offset += speed * dt / 1000;
      while (offset >= step) {
        offset -= step;
        candles.shift();
        candles.push(nextCandle());
      }
      draw();
    }
    requestAnimationFrame(frame);
  }

  var timer;
  window.addEventListener('resize', function () {
    clearTimeout(timer); timer = setTimeout(resize, 150);
  });
  resize();
  if (!reduce) requestAnimationFrame(frame);
})();
