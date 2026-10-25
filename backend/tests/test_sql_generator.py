from app.core.sql_generator import FEW_SHOT_EXAMPLES, build_messages


def test_prompt_requires_only_requested_columns_and_no_select_star():
    messages = build_messages("List customers from Germany", "TABLE customers")
    system_prompt = messages[0]["content"]

    assert "Select only the columns needed" in system_prompt
    assert "Never use SELECT *" in system_prompt


def test_few_shot_examples_cover_projection_and_money_rounding():
    examples = "\n".join(example["response"] for example in FEW_SHOT_EXAMPLES)

    assert '"sql": "SELECT product_name FROM products WHERE discontinued = 1;"' in examples
    assert "SELECT company_name, contact_name FROM customers" in examples
    assert "ROUND(SUM(od.unit_price * od.quantity * (1 - od.discount))::numeric, 2)" in examples


def test_few_shot_examples_show_requested_order_detail_columns():
    example = next(
        item
        for item in FEW_SHOT_EXAMPLES
        if "quantity ordered" in item["question"]
    )

    assert "p.product_name, od.quantity, od.unit_price, od.discount" in example["response"]


def test_few_shot_examples_cover_useful_default_list_projections():
    examples = "\n".join(example["response"] for example in FEW_SHOT_EXAMPLES)

    assert "SELECT order_id, order_date, freight FROM orders" in examples
    assert "SELECT o.order_id, o.order_date, o.customer_id" in examples
    assert "SELECT c.company_name, c.contact_name FROM customers" in examples
    assert "units_in_stock, units_on_order" in examples


def test_prompt_spells_out_complete_projection_and_aggregate_rules():
    system_prompt = build_messages("question", "TABLE orders")[0]["content"]

    assert "return every requested field and no extra fields" in system_prompt
    assert "Do not round freight totals" in system_prompt
    assert "Do not ask for clarification just because a list request" in system_prompt
    assert '"kind":"sql"' in system_prompt
