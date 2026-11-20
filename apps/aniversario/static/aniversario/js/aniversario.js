/* Aniversario: mejoras progresivas. Todo funciona sin JS; esto solo agrega
   cuenta regresiva, galería, bloqueo de doble envío y refresco del conteo. */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  // Cuenta regresiva
  var cd = $("#cd");
  if (cd && cd.dataset.fin) {
    var fin = new Date(cd.dataset.fin).getTime();
    var celdas = $$("b", cd);
    var tick = function () {
      var m = Math.max(0, fin - Date.now());
      [864e5, 36e5, 6e4, 1e3].forEach(function (k, j) {
        var v = Math.floor(m / k); m -= v * k;
        if (celdas[j]) celdas[j].textContent = String(v).padStart(2, "0");
      });
    };
    tick(); setInterval(tick, 1000);
  }

  // Galería del perfil
  var gal = $("[data-galeria]");
  if (gal) {
    var imgs = $$(".ph img", gal), pos = $("[data-pos]", gal), thumbs = $$(".th button", gal), i = 0;
    var ver = function (n) {
      if (!imgs.length) return;
      i = (n + imgs.length) % imgs.length;
      imgs.forEach(function (im, j) { im.classList.toggle("on", j === i); });
      thumbs.forEach(function (b, j) { b.setAttribute("aria-pressed", j === i ? "true" : "false"); });
      if (pos) pos.textContent = (i + 1) + " / " + imgs.length;
    };
    gal.addEventListener("click", function (e) {
      var b = e.target.closest("[data-g]");
      if (!b) return;
      var g = b.dataset.g;
      ver(g === "prev" ? i - 1 : g === "next" ? i + 1 : +g);
    });
  }

  // Pasos: el botón "Siguiente" se habilita al elegir
  var paso = $("[data-paso]");
  if (paso) {
    var sig = $("[data-siguiente]", paso);
    var act = function () { sig.disabled = !$("input[type=radio]:checked", paso); };
    paso.addEventListener("change", act); act();
  }

  // Evita doble envío del voto
  var conf = $("[data-confirmar]");
  if (conf) {
    conf.addEventListener("submit", function () {
      var b = $("button[type=submit]", conf);
      if (b) { b.disabled = true; b.textContent = "Registrando tu voto…"; }
    });
  }

  // Conteo en vivo
  if ($("[data-vt]")) {
    var refrescar = function () {
      if (document.hidden) return;
      fetch("/api/conteo/", { headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (!d || !d.visible) return;
          $$("[data-vt]").forEach(function (el) {
            var x = d.candidatos[el.dataset.vt];
            if (!x) return;
            $("[data-votos]", el).textContent = x.votos.toLocaleString("es-PE");
            $("[data-pc]", el).textContent = x.pct + "% · " + x.puesto + ".º lugar";
            $(".vbar i", el).style.width = x.pct + "%";
          });
        })
        .catch(function () {});
    };
    setInterval(refrescar, 25000);
  }
})();
