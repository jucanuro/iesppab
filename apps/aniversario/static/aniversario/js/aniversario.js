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

  // Carruseles de fotos (tarjetas y perfil)
  $$("[data-car]").forEach(function (car) {
    var pista = $("[data-pista]", car);
    var n = pista ? pista.children.length : 0;
    if (n < 2) return;
    var puntos = $$(".puntos i", car), pos = $("[data-pos]", car);
    var gal = car.closest(".gal"), thumbs = gal ? $$(".th button", gal) : [];
    var actual = function () { return Math.round(pista.scrollLeft / pista.clientWidth); };
    var ir = function (k) {
      k = (k + n) % n;
      pista.scrollTo({ left: k * pista.clientWidth });
    };
    var marcar = function () {
      var k = actual();
      puntos.forEach(function (p, j) { p.classList.toggle("on", j === k); });
      thumbs.forEach(function (b, j) { b.setAttribute("aria-pressed", j === k ? "true" : "false"); });
      if (pos) pos.textContent = (k + 1) + " / " + n;
    };
    var t;
    pista.addEventListener("scroll", function () { clearTimeout(t); t = setTimeout(marcar, 60); }, { passive: true });
    var mover = function (e) {
      var b = e.target.closest("[data-mover]");
      if (!b) return;
      e.preventDefault(); e.stopPropagation();  // no abrir el perfil ni marcar el voto
      ir(actual() + (+b.dataset.mover));
    };
    car.addEventListener("click", mover);
    car.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") mover(e);
    });
    thumbs.forEach(function (b) {
      b.addEventListener("click", function () { ir(+b.dataset.ir); });
    });
  });

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
