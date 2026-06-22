"""Smoke-test every SEAO MCP tool against DATA/seao.db using the analyst's own
example questions. Run: python test_mcp_tools.py"""

import seao_mcp_server as s


def show(title, text):
    print("\n" + "=" * 78)
    print("### " + title)
    print("=" * 78)
    print(text)


# 1. Schema / reference
show("describe_schema() overview", s.describe_schema())
show("describe_schema('award')", s.describe_schema("award"))

# 2. run_sql (read-only) + a rejection
show("run_sql SELECT", s.run_sql(
    "SELECT procurement_method, COUNT(*) n FROM tender GROUP BY 1 ORDER BY n DESC"))
show("run_sql rejects write", s.run_sql("DELETE FROM award"))

# 3. search_processes -- Marguerite-Bourgeoys security tenders
show("search_processes(buyer=Marguerite-Bourgeoys, text=securite)",
     s.search_processes(buyer="Marguerite-Bourgeoys", text="securite", limit=10))

# 4. incumbent -- "Who held the security contract at CSS Marguerite-Bourgeoys?"
show("incumbent(Marguerite-Bourgeoys, securite)",
     s.incumbent(buyer="Marguerite-Bourgeoys", service="securite"))

# 5. price_benchmark -- guarding (UNSPSC 90152100) in Montreal (area 6)
show("price_benchmark(90152100, area 6)",
     s.price_benchmark(unspsc="90152100", delivery_area="6"))

# 6. supplier_profile -- Garda win rate
show("supplier_profile(Garda)", s.supplier_profile("Garda"))

# 7. head_to_head -- Commissionnaires vs Neptune
show("head_to_head(Commissionnaires, Neptune)",
     s.head_to_head("Commissionnaires", "Neptune"))

# 8. buyer_profile -- Surete du Quebec rotation/loyalty
show("buyer_profile(Surete du Quebec)", s.buyer_profile("Surete du Quebec"))

# 9. market_overview -- TAM 2020 vs 2021 + top-3 concentration
show("market_overview(top_n=3)", s.market_overview(top_n=3))

print("\n\nALL TOOLS RAN.")
