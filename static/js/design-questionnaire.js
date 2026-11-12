(function () {
    var root = document.getElementById("dq");
    var form = document.getElementById("dq-form");
    var stage = document.getElementById("dq-stage");
    var visual = document.getElementById("dq-visual");
    var trustEl = document.getElementById("dq-trust");
    var whoEl = document.getElementById("dq-who");
    var progress = document.getElementById("dq-progress");
    var backBtn = document.getElementById("dq-back");
    var nextBtn = document.getElementById("dq-next");
    var pctTotal = document.getElementById("dq-pct-total");
    var firm = root.getAttribute("data-firm") || "EP Intelligence";

    var STEPS = [
        { id: "people", label: "The trust" },
        { id: "health", label: "Health care" },
        { id: "wishes", label: "Wishes" },
        { id: "charge", label: "Who is in charge" },
        { id: "living", label: "While living" },
        { id: "survivor", label: "At the first death", couple: true },
        { id: "assets", label: "Gifts and property" },
        { id: "distribution", label: "Who inherits" },
        { id: "review", label: "Finished" }
    ];

    var BAR = ["#ea580c", "#c2410c", "#fb923c", "#9a3412", "#fdba74"];
    var stepId = "people";
    var shownStep = "";
    var frame = 0;

    function val(name) {
        var el = form.elements[name];
        if (!el) return "";
        if (typeof el.length === "number" && el[0] && el[0].tagName === "INPUT") {
            var picked = null;
            for (var i = 0; i < el.length; i++) {
                if (el[i].checked) picked = el[i];
            }
            if (picked) return picked.value;
            if (el[0].type === "radio") return "";
        }
        if (el.type === "checkbox") return el.checked ? "yes" : "";
        return String(el.value || "").trim();
    }

    function lines(prefix, count) {
        var out = [];
        for (var i = 1; i <= count; i++) {
            var text = val(prefix + i);
            if (text) out.push(text);
        }
        return out;
    }

    function couple() {
        return val("household") !== "single";
    }

    function steps() {
        return STEPS.filter(function (step) {
            return couple() || !step.couple;
        });
    }

    function yn(value) {
        if (value === "yes") return "Yes";
        if (value === "no") return "No";
        return "—";
    }

    function pace(value) {
        if (value === "slow") return "Slow";
        if (value === "middle") return "Middle";
        if (value === "express") return "Express";
        return "—";
    }

    function doctors(value) {
        if (value === "1") return "1 doctor";
        if (value === "2") return "2 doctors";
        return "—";
    }

    function organ(value) {
        if (value === "yes") return "Yes";
        if (value === "no") return "No";
        if (value === "prohibit") return "Prohibit";
        return "—";
    }

    function disposition(value) {
        if (value === "cremation") return "Cremation";
        if (value === "burial") return "Burial";
        return "—";
    }

    function passLabel(value) {
        if (value === "stirpes") return "Per stirpes";
        if (value === "rata") return "Pro rata";
        return "";
    }

    function disinheritLabel(value) {
        if (value === "decisions") return "Decisions only";
        if (value === "money") return "Money only";
        if (value === "both") return "Money and decisions";
        return "—";
    }

    function read() {
        var people = [];
        if (val("client_name")) people.push(val("client_name"));
        if (couple() && val("spouse_name")) people.push(val("spouse_name"));
        var beneficiaries = [];
        for (var i = 1; i <= 5; i++) {
            beneficiaries.push({
                name: val("ben_name_" + i),
                pct: val("ben_pct_" + i),
                pass: val("ben_pass_" + i)
            });
        }
        return {
            couple: couple(),
            client: val("client_name"),
            spouse: val("spouse_name"),
            trust: val("trust_name"),
            people: people,
            beneficiaries: beneficiaries
        };
    }

    function el(tag, cls, text) {
        var node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text != null && text !== "") node.textContent = text;
        return node;
    }

    function numbered(items) {
        var list = el("ol", "dq-agent-list");
        items.forEach(function (text) {
            list.appendChild(el("li", "is-filled", text));
        });
        return list;
    }

    function plainList(items) {
        var list = el("ul", "dq-plain-list");
        items.forEach(function (item) {
            list.appendChild(el("li", "", item));
        });
        return list;
    }

    function personCard(role, name, agents) {
        var card = el("article", "dq-person");
        card.appendChild(el("p", "dq-role", role));
        if (name) card.appendChild(el("h3", "", name));
        if (agents.length) card.appendChild(numbered(agents));
        return card;
    }

    function wishCard(name, prefix) {
        var card = el("article", "dq-card");
        card.appendChild(el("p", "dq-role", name || "Client"));
        var rail = el("div", "dq-rail");
        [["slow", "Slow"], ["middle", "Middle"], ["express", "Express"]].forEach(function (pair) {
            var on = val(prefix + "_speed") === pair[0];
            rail.appendChild(el("div", "dq-rail-item" + (on ? " is-on" : ""), pair[1]));
        });
        card.appendChild(rail);
        var bits = [];
        if (val(prefix + "_doctors")) bits.push(doctors(val(prefix + "_doctors")));
        if (val(prefix + "_organ")) bits.push("Organ donation: " + organ(val(prefix + "_organ")));
        if (val(prefix + "_disposition")) bits.push(disposition(val(prefix + "_disposition")));
        if (val(prefix + "_wishes")) bits.push(val(prefix + "_wishes"));
        if (bits.length) card.appendChild(plainList(bits));
        return card;
    }

    function renderHealth() {
        var row = el("div", "dq-people-row");
        row.appendChild(personCard("Health care agents", val("client_name"), lines("client_agent_", 4)));
        if (couple()) {
            row.appendChild(personCard("Health care agents", val("spouse_name"), lines("spouse_agent_", 4)));
        }
        return row;
    }

    function renderWishes() {
        var row = el("div", "dq-people-row");
        row.appendChild(wishCard(val("client_name"), "client"));
        if (couple()) row.appendChild(wishCard(val("spouse_name"), "spouse"));
        return row;
    }

    function renderCharge() {
        var stack = el("div", "dq-stack");
        var now = el("article", "dq-card");
        now.appendChild(el("p", "dq-role", "Alive and well"));
        now.appendChild(el("h3", "", read().people.join(" and ") || "You"));
        now.appendChild(el("p", "", "You stay in control."));
        stack.appendChild(now);

        var next = el("article", "dq-card");
        next.appendChild(el("p", "dq-role", "Disability"));
        var note = "Agents confirm incapacity.";
        if (val("cotrustee") === "yes") {
            note += couple() && val("cotrustee_when") === "second"
                ? " Co-trustee at the 2nd disability or death."
                : " Co-trustee steps in.";
        }
        if (couple() && val("spouse_alone") === "yes") note += " Spouse may serve alone.";
        next.appendChild(el("p", "", note));
        var trustees = lines("trustee_", 4);
        if (trustees.length) {
            next.appendChild(el("p", "dq-role", "Trustees"));
            next.appendChild(numbered(trustees));
        }
        var poa = lines("poa_", 4);
        if (poa.length) {
            next.appendChild(el("p", "dq-role", "Financial POA"));
            next.appendChild(numbered(poa));
        }
        stack.appendChild(next);

        var guardians = lines("guardian_", 3);
        if (guardians.length) {
            var guard = el("article", "dq-card");
            guard.appendChild(el("p", "dq-role", "Guardians"));
            guard.appendChild(numbered(guardians));
            stack.appendChild(guard);
        }
        return stack;
    }

    function renderLiving() {
        var stack = el("div", "dq-stack");
        var alive = el("article", "dq-card");
        alive.appendChild(el("p", "dq-role", "Alive and well"));
        alive.appendChild(el("h3", "", "In control"));
        alive.appendChild(el("p", "", "No tax change. Reported under your SSN."));
        alive.appendChild(el("p", "dq-warn", "Not lawsuit protected."));
        stack.appendChild(alive);

        var chips = el("div", "dq-pills");
        if (val("homecare")) chips.appendChild(el("span", "dq-pill", "Homecare " + yn(val("homecare"))));
        if (val("pets")) chips.appendChild(el("span", "dq-pill", "Pets " + yn(val("pets"))));
        if (val("protector")) chips.appendChild(el("span", "dq-pill", "Trust protector " + yn(val("protector"))));
        if (chips.childNodes.length) stack.appendChild(chips);

        if (val("children")) {
            var kids = el("article", "dq-card");
            kids.appendChild(el("p", "dq-role", "Children"));
            kids.appendChild(el("h3", "", val("children")));
            kids.appendChild(el("p", "", "Health, education, and maintenance."));
            stack.appendChild(kids);
        }

        var priority = lines("priority_", 3);
        if (priority.length) {
            var order = el("article", "dq-card");
            order.appendChild(el("p", "dq-role", "Priority"));
            order.appendChild(numbered(priority));
            stack.appendChild(order);
        }
        return stack;
    }

    function trustCard(kicker, title, body) {
        var card = el("article", "dq-trustcard");
        card.appendChild(el("p", "dq-role", kicker));
        card.appendChild(el("h3", "", title));
        card.appendChild(el("p", "", body));
        return card;
    }

    function renderSurvivor() {
        var wrap = el("div", "");
        var pills = el("div", "dq-pills");
        if (val("remarriage") === "yes") pills.appendChild(el("span", "dq-pill", "Remarriage protection"));
        if (val("spouse_trustee") === "yes") pills.appendChild(el("span", "dq-pill", "Spouse stays trustee"));
        if (val("prenuptial") === "yes") pills.appendChild(el("span", "dq-pill", "Prenuptial agreement"));
        if (pills.childNodes.length) wrap.appendChild(pills);
        wrap.appendChild(el("p", "dq-branch-label", "At the first death"));
        wrap.appendChild(el("div", "dq-branch-bar"));
        var row = el("div", "dq-trust-row");
        row.appendChild(trustCard("Revocable", "Survivor's trust", "Spouse controls it and can change it."));
        row.appendChild(trustCard("Irrevocable", "Marital trust", "All income to the spouse. Remainder is taxable."));
        row.appendChild(trustCard("Irrevocable", "Family trust", "Needs only. Out of the survivor's estate."));
        wrap.appendChild(row);
        return wrap;
    }

    function groupCard(title, items) {
        if (!items.length) return null;
        var card = el("article", "dq-card");
        card.appendChild(el("p", "dq-role", title));
        card.appendChild(plainList(items));
        return card;
    }

    function renderAssets() {
        var stack = el("div", "dq-stack");
        [
            ["Retirement accounts", lines("retirement_", 4)],
            ["Life insurance", lines("insurance_", 4)],
            ["Gifts", lines("gift_", 6)],
            ["Disinherit", lines("disinherit_", 4)]
        ].forEach(function (pair) {
            var card = groupCard(pair[0], pair[1]);
            if (card) stack.appendChild(card);
        });
        var facts = [];
        if (val("lp") === "yes") facts.push("Limited partnership" + (val("lp_count") ? " · " + val("lp_count") : ""));
        if (val("llc") === "yes") facts.push("LLC" + (val("llc_count") ? " · " + val("llc_count") : ""));
        if (val("subtrust") === "yes") facts.push("Subtrust");
        if (val("no_contest") === "yes") facts.push("No-contest clause");
        if (couple() && val("marital_agreement") === "yes") facts.push("Marital agreement");
        if (val("deeds_tod") === "yes") facts.push("TOD deeds");
        if (val("deeds_trust") === "yes") facts.push("Deeds in trust");
        if (val("entities")) facts.push(val("entities"));
        if (val("tangible")) facts.push("Tangible: " + val("tangible"));
        if (val("disinherit_mode")) facts.push(disinheritLabel(val("disinherit_mode")));
        var extra = groupCard("Also in the plan", facts);
        if (extra) stack.appendChild(extra);
        return stack;
    }

    function renderDistribution() {
        var wrap = el("div", "");
        wrap.appendChild(el("p", "dq-paid", "Debts, taxes, and gifts first"));
        var bar = el("div", "dq-bar");
        var listed = read().beneficiaries.filter(function (row) {
            return row.name || row.pct;
        });
        var assigned = 0;
        listed.forEach(function (row) {
            var n = parseFloat(row.pct);
            if (!isNaN(n) && n > 0) assigned += n;
        });
        var scale = assigned > 100 ? 100 / assigned : 1;
        if (!listed.length) {
            var empty = el("i");
            empty.style.width = "100%";
            empty.style.background = "rgba(0,0,0,0.06)";
            bar.appendChild(empty);
        } else {
            listed.forEach(function (row, index) {
                var n = parseFloat(row.pct);
                var width = !isNaN(n) && n > 0 ? n * scale : (assigned > 0 ? 0 : 100 / listed.length);
                if (width <= 0) return;
                var seg = el("i");
                seg.style.width = width + "%";
                seg.style.background = BAR[index % BAR.length];
                bar.appendChild(seg);
            });
            if (assigned > 0 && assigned < 100) {
                var rest = el("i");
                rest.style.width = (100 - assigned) + "%";
                rest.style.background = "rgba(0,0,0,0.08)";
                bar.appendChild(rest);
            }
        }
        wrap.appendChild(bar);
        var list = el("ul", "dq-share-list");
        listed.forEach(function (row) {
            var li = el("li");
            var name = el("span", "", row.name || "—");
            var meta = el("small", "", [row.pct ? row.pct + "%" : "", passLabel(row.pass)].filter(Boolean).join(" · "));
            li.appendChild(name);
            li.appendChild(meta);
            list.appendChild(li);
        });
        wrap.appendChild(list);
        if (val("minor_age")) wrap.appendChild(el("p", "dq-note", "Outright at " + val("minor_age") + "."));
        if (val("remote") === "heirs") wrap.appendChild(el("p", "dq-note", "Otherwise, heirs at law (Texas)."));
        if (val("remote") === "charity") wrap.appendChild(el("p", "dq-note", "Otherwise, " + (val("remote_charity") || "a charity") + "."));
        if (val("remote") === "named") {
            var names = [val("remote_named_1"), val("remote_named_2")].filter(Boolean).join(" and ");
            if (names) wrap.appendChild(el("p", "dq-note", "Otherwise, " + names + "."));
        }
        return wrap;
    }

    var VISUALS = {
        health: renderHealth,
        wishes: renderWishes,
        charge: renderCharge,
        living: renderLiving,
        survivor: renderSurvivor,
        assets: renderAssets,
        distribution: renderDistribution,
        review: renderDistribution
    };

    function paint(entered) {
        var data = read();
        root.classList.toggle("is-couple", data.couple);
        root.classList.toggle("is-single", !data.couple);
        root.classList.toggle("is-cotrustee", val("cotrustee") === "yes");
        root.classList.toggle("is-remote-charity", val("remote") === "charity");
        root.classList.toggle("is-remote-named", val("remote") === "named");
        stage.classList.toggle("is-hero", stepId === "people");
        stage.classList.toggle("is-settled", stepId !== "people");
        trustEl.textContent = data.trust;
        whoEl.textContent = data.people.join(" and ");

        var clientHeading = document.getElementById("dq-client-agent-heading");
        var spouseHeading = document.getElementById("dq-spouse-agent-heading");
        var clientWish = document.getElementById("dq-client-wish-heading");
        var spouseWish = document.getElementById("dq-spouse-wish-heading");
        if (clientHeading) clientHeading.textContent = (data.client || "Client") + "'s agents";
        if (spouseHeading) spouseHeading.textContent = (data.spouse || "Spouse") + "'s agents";
        if (clientWish) clientWish.textContent = data.client || "Client";
        if (spouseWish) spouseWish.textContent = data.spouse || "Spouse";

        var assigned = 0;
        var anyPct = false;
        data.beneficiaries.forEach(function (row) {
            var n = parseFloat(row.pct);
            if (!isNaN(n)) {
                anyPct = true;
                assigned += n;
            }
        });
        pctTotal.textContent = anyPct ? "Assigned " + Math.round(assigned * 10) / 10 + "%" : "";

        document.querySelectorAll("[data-step]").forEach(function (section) {
            section.hidden = section.getAttribute("data-step") !== stepId;
        });

        var list = steps();
        progress.replaceChildren();
        var here = list.findIndex(function (step) { return step.id === stepId; });
        list.forEach(function (step, index) {
            var mark = step.id === stepId ? " is-now" : index < here ? " is-done" : "";
            var dot = el("button", "dq-dot" + mark, "");
            dot.type = "button";
            dot.title = step.label;
            dot.setAttribute("aria-label", step.label);
            if (step.id === stepId) dot.setAttribute("aria-current", "step");
            dot.addEventListener("click", function () { go(step.id); });
            progress.appendChild(dot);
        });

        var atStart = list[0].id === stepId;
        var atEnd = list[list.length - 1].id === stepId;
        backBtn.classList.toggle("is-hidden", atStart);
        nextBtn.classList.toggle("is-hidden", atEnd);
        backBtn.tabIndex = atStart ? -1 : 0;
        nextBtn.tabIndex = atEnd ? -1 : 0;

        var draw = VISUALS[stepId];
        if (draw) {
            visual.replaceChildren(draw());
            if (entered) {
                visual.classList.remove("is-in");
                void visual.offsetWidth;
                visual.classList.add("is-in");
            }
        } else {
            visual.replaceChildren();
        }

        if (entered) {
            document.querySelector(".dq-scroll").scrollTop = 0;
            stage.scrollTop = 0;
            var section = document.querySelector('[data-step="' + stepId + '"]');
            var fields = section ? section.querySelectorAll("input, textarea, button") : [];
            var field = Array.prototype.find.call(fields, function (node) {
                return node.id !== "dq-download" && node.offsetParent !== null;
            });
            if (field) field.focus({ preventScroll: true });
            shownStep = stepId;
        }
    }

    function go(id) {
        var list = steps();
        if (!list.some(function (step) { return step.id === id; })) id = list[0].id;
        var entered = id !== stepId;
        stepId = id;
        paint(entered || shownStep !== id);
    }

    function move(dir) {
        var list = steps();
        var index = list.findIndex(function (step) { return step.id === stepId; });
        var next = list[index + dir];
        if (next) go(next.id);
    }

    form.addEventListener("submit", function (event) { event.preventDefault(); });
    form.addEventListener("input", function () {
        cancelAnimationFrame(frame);
        frame = requestAnimationFrame(function () { paint(false); });
    });
    form.addEventListener("change", function (event) {
        if (event.target.name === "household" && !steps().some(function (step) { return step.id === stepId; })) {
            go("assets");
            return;
        }
        paint(false);
    });
    backBtn.addEventListener("click", function () { move(-1); });
    nextBtn.addEventListener("click", function () { move(1); });

    function pdfEscape(text) {
        var clean = String(text || "")
            .replace(/[\u2013\u2014]/g, "-")
            .replace(/[\u2018\u2019]/g, "'")
            .replace(/[\u201C\u201D]/g, '"')
            .replace(/\s+/g, " ")
            .trim();
        var out = "";
        for (var i = 0; i < clean.length; i++) {
            var c = clean.charCodeAt(i);
            if (c === 92 || c === 40 || c === 41) out += "\\" + clean.charAt(i);
            else if (c >= 32 && c <= 126) out += clean.charAt(i);
            else out += "?";
        }
        return "(" + out + ")";
    }

    function wrap(text, width) {
        var words = String(text || "").split(/\s+/).filter(Boolean);
        if (!words.length) return [""];
        var lines = [];
        var cur = "";
        words.forEach(function (word) {
            while (word.length > width) {
                if (cur) {
                    lines.push(cur);
                    cur = "";
                }
                lines.push(word.slice(0, width));
                word = word.slice(width);
            }
            var next = cur ? cur + " " + word : word;
            if (next.length > width && cur) {
                lines.push(cur);
                cur = word;
            } else {
                cur = next;
            }
        });
        if (cur) lines.push(cur);
        return lines;
    }

    function row(label, value) {
        return label + ": " + (value || "—");
    }

    function listRow(label, items) {
        return row(label, items.length ? items.join("; ") : "");
    }

    function questionnaireBlocks() {
        var data = read();
        var blocks = [
            { text: "Revocable Living Trust Design", size: 18, bold: true, color: "0.918 0.345 0.047", width: 48, leading: 24 },
            { text: firm, size: 11, width: 80, leading: 16 },
            { text: new Date().toLocaleDateString("en-US", { year: "numeric", month: "long", day: "numeric" }), size: 11, width: 80, leading: 18 }
        ];
        function section(title, rows) {
            blocks.push({ text: title, size: 13, bold: true, width: 64, spaceBefore: 14, leading: 18 });
            rows.forEach(function (line) {
                blocks.push({ text: line, size: 10.5, width: 85, leading: 15 });
            });
        }
        section("The trust", [
            row("Household", data.couple ? "Couple" : "Single"),
            row("Client", data.client),
            data.couple ? row("Spouse", data.spouse) : "",
            row("Trust name", data.trust)
        ].filter(Boolean));
        var healthRows = [listRow((data.client || "Client") + "'s agents", lines("client_agent_", 4))];
        if (data.couple) healthRows.push(listRow((data.spouse || "Spouse") + "'s agents", lines("spouse_agent_", 4)));
        section("Health care", healthRows);
        function wishes(prefix, name) {
            return [
                name,
                row("Pace", pace(val(prefix + "_speed"))),
                row("Doctors", doctors(val(prefix + "_doctors"))),
                row("Organ donation", organ(val(prefix + "_organ"))),
                row("Disposition", disposition(val(prefix + "_disposition"))),
                row("Specific requests", val(prefix + "_wishes"))
            ];
        }
        section("Wishes", wishes("client", data.client || "Client").concat(
            data.couple ? wishes("spouse", data.spouse || "Spouse") : []
        ));
        section("Who is in charge", [
            row("Name a co-trustee", yn(val("cotrustee"))),
            data.couple && val("cotrustee") === "yes" ? row("Co-trustee serves upon", val("cotrustee_when") === "first" ? "1st disability or death" : val("cotrustee_when") === "second" ? "2nd disability or death" : "—") : "",
            !data.couple && val("cotrustee") === "yes" ? "At disability or death." : "",
            data.couple && val("cotrustee") === "yes" ? row("Spouse may serve alone", yn(val("spouse_alone"))) : "",
            listRow("Trustees", lines("trustee_", 4)),
            listRow("Financial POA", lines("poa_", 4)),
            row("Same as trustees", yn(val("executor_same"))),
            listRow("Guardians", lines("guardian_", 3))
        ].filter(Boolean));
        section("While living", [
            row("Homecare", yn(val("homecare"))),
            row("Children", val("children")),
            "Health, education, and maintenance.",
            row("Pets", yn(val("pets"))),
            row("Trust protector", yn(val("protector"))),
            listRow("Priority", lines("priority_", 3)),
            row("Specific requests", val("admin_notes")),
            row("Financial advisor", val("advisor")),
            row("CPA", val("cpa"))
        ]);
        if (data.couple) {
            section("At the first death", [
                row("Remarriage protection", yn(val("remarriage"))),
                row("Spouse stays trustee", yn(val("spouse_trustee"))),
                row("Prenuptial agreement", yn(val("prenuptial")))
            ]);
        }
        section("Gifts and property", [
            listRow("Retirement accounts", lines("retirement_", 4)),
            listRow("Life insurance", lines("insurance_", 4)),
            row("Limited partnership", yn(val("lp")) + (val("lp") === "yes" && val("lp_count") ? " (" + val("lp_count") + ")" : "")),
            row("LLC", yn(val("llc")) + (val("llc") === "yes" && val("llc_count") ? " (" + val("llc_count") + ")" : "")),
            row("Subtrust", yn(val("subtrust"))),
            row("No-contest clause", yn(val("no_contest"))),
            data.couple ? row("Marital agreement", yn(val("marital_agreement"))) : "",
            row("Deeds, transfer on death", val("deeds_tod") === "yes" ? "Yes" : "No"),
            row("Deeds, in trust", val("deeds_trust") === "yes" ? "Yes" : "No"),
            row("Entities", val("entities")),
            listRow("Gifts", lines("gift_", 6)),
            row("Limited power of appointment, terms", yn(val("lpoa_terms"))),
            row("Limited power of appointment, percentage limit", yn(val("lpoa_percent"))),
            row("Limited power of appointment, charities", yn(val("lpoa_charity"))),
            row("Disinherit", disinheritLabel(val("disinherit_mode"))),
            listRow("Disinherit names", lines("disinherit_", 4)),
            row("Tangible property", val("tangible"))
        ].filter(Boolean));
        var shareRows = data.beneficiaries.map(function (share, index) {
            return row("Beneficiary " + (index + 1), [share.name, share.pct ? share.pct + "%" : "", passLabel(share.pass)].filter(Boolean).join(", "));
        });
        var remote = "—";
        if (val("remote") === "heirs") remote = "Heirs at law (Texas)";
        if (val("remote") === "charity") remote = "Charity: " + (val("remote_charity") || "—");
        if (val("remote") === "named") remote = "Named: " + ([val("remote_named_1"), val("remote_named_2")].filter(Boolean).join("; ") || "—");
        section(data.couple ? "Final distribution, at the second death" : "Final distribution", shareRows.concat([
            row("Provisions", val("provisions")),
            row("Minor age", val("minor_age")),
            row("If none survive", remote)
        ]));
        return blocks;
    }

    function buildPdf(blocks) {
        var pages = [];
        var page = [];
        var y = 744;
        function pushPage() {
            if (page.length) pages.push(page);
            page = [];
            y = 744;
        }
        blocks.forEach(function (block) {
            var size = block.size || 10.5;
            var leading = block.leading || size + 4;
            if (block.spaceBefore) {
                if (y - block.spaceBefore < 54) pushPage();
                else y -= block.spaceBefore;
            }
            wrap(block.text, block.width || 90).forEach(function (line) {
                if (y - leading < 54) pushPage();
                page.push({
                    text: line,
                    x: 54,
                    y: y,
                    size: size,
                    bold: !!block.bold,
                    color: block.color || "0 0 0"
                });
                y -= leading;
            });
        });
        if (page.length) pages.push(page);

        var objects = [];
        function add(body) {
            objects.push(body);
            return objects.length;
        }
        add("<< /Type /Catalog /Pages 2 0 R >>");
        add("");
        var pageIds = [];
        var fontRegular = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>");
        var fontBold = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>");
        pages.forEach(function (lines, index) {
            var commands = ["BT"];
            lines.forEach(function (line) {
                commands.push(line.color + " rg");
                commands.push((line.bold ? "/F2 " : "/F1 ") + line.size + " Tf");
                commands.push("1 0 0 1 " + line.x + " " + line.y + " Tm");
                commands.push(pdfEscape(line.text) + " Tj");
            });
            commands.push("0 0 0 rg");
            commands.push("/F1 9 Tf");
            commands.push("1 0 0 1 54 32 Tm");
            commands.push(pdfEscape("Page " + (index + 1) + " of " + pages.length) + " Tj");
            commands.push("ET");
            var stream = commands.join("\n");
            var contentId = add("<< /Length " + stream.length + " >>\nstream\n" + stream + "\nendstream");
            pageIds.push(add(
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 " +
                fontRegular + " 0 R /F2 " + fontBold + " 0 R >> >> /Contents " + contentId + " 0 R >>"
            ));
        });
        objects[1] = "<< /Type /Pages /Count " + pageIds.length + " /Kids [" + pageIds.map(function (id) {
            return id + " 0 R";
        }).join(" ") + "] >>";

        var out = "%PDF-1.4\n";
        var offsets = [0];
        objects.forEach(function (body, index) {
            offsets.push(out.length);
            out += (index + 1) + " 0 obj\n" + body + "\nendobj\n";
        });
        var xref = out.length;
        out += "xref\n0 " + offsets.length + "\n";
        out += "0000000000 65535 f \n";
        for (var i = 1; i < offsets.length; i++) {
            out += String(offsets[i]).padStart(10, "0") + " 00000 n \n";
        }
        out += "trailer << /Size " + offsets.length + " /Root 1 0 R >>\nstartxref\n" + xref + "\n%%EOF";
        return out;
    }

    document.getElementById("dq-download").addEventListener("click", function () {
        var data = read();
        var pdf = buildPdf(questionnaireBlocks());
        var blob = new Blob([pdf], { type: "application/pdf" });
        var link = document.createElement("a");
        var base = (data.trust || "Design Questionnaire").replace(/[^\w\s-]+/g, "").trim() || "Design Questionnaire";
        link.href = URL.createObjectURL(blob);
        link.download = base + " - Design Questionnaire.pdf";
        document.body.appendChild(link);
        link.click();
        setTimeout(function () {
            URL.revokeObjectURL(link.href);
            link.remove();
        }, 60000);
    });

    paint(true);
})();
