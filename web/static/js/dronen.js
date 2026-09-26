document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-tab-target]").forEach(button => {
        button.addEventListener("click", () => {
            document.querySelectorAll("[data-tab-target]").forEach(x => x.classList.remove("active"));
            button.classList.add("active");

            document.querySelector("#tab-clients").classList.add("d-none");
            document.querySelector("#tab-aps").classList.add("d-none");

            const target = button.dataset.tabTarget;
            document.querySelector("#tab-" + target).classList.remove("d-none");
        });
    });

    const clientSearch = document.querySelector("#clientSearch");
    const clientClass = document.querySelector("#clientClass");

    function filterClients() {
        if (!clientSearch) return;

        const query = clientSearch.value.toLowerCase();
        const selected = clientClass ? clientClass.value : "";

        const rank = {
            "background": 0,
            "observe": 1,
            "interesting": 2,
            "candidate": 3
        };

        document.querySelectorAll("#clientTable tbody tr").forEach(row => {
            const text = row.innerText.toLowerCase();
            const cls = row.dataset.class || "background";

            const matchesText = text.includes(query);
            const matchesClass = (
                !selected ||
                (rank[cls] ?? 0) >= (rank[selected] ?? 0)
            );

            row.style.display = matchesText && matchesClass ? "" : "none";
        });
    }

    if (clientSearch) clientSearch.addEventListener("input", filterClients);
    if (clientClass) clientClass.addEventListener("change", filterClients);

    const apSearch = document.querySelector("#apSearch");

    if (apSearch) {
        apSearch.addEventListener("input", () => {
            const query = apSearch.value.toLowerCase();

            document.querySelectorAll("#apTable tbody tr").forEach(row => {
                row.style.display = row.innerText.toLowerCase().includes(query)
                    ? ""
                    : "none";
            });
        });
    }
});
