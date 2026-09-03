import base64
import io
from datetime import datetime

import bcrypt
import pandas as pd
import psycopg2
import streamlit as st
from PIL import Image
from reportlab.lib.pagesizes import landscape, letter
from reportlab.pdfgen import canvas

try:
    from streamlit_drawable_canvas import st_canvas
    CANVAS_AVAILABLE = True
except Exception:
    CANVAS_AVAILABLE = False

st.set_page_config(
    page_title="Sistema de Mini Lecciones",
    page_icon="🎓",
    layout="wide"
)


def get_connection():
    return psycopg2.connect(
        st.secrets["DATABASE_URL"],
        sslmode="require"
    )


def hash_password(password: str) -> str:
    return bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt()
    ).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(
        password.encode("utf-8"),
        password_hash.encode("utf-8")
    )


def get_lessons() -> pd.DataFrame:
    conn = get_connection()
    try:
        query = """
            SELECT
                l.id,
                l.title AS lesson_title,
                l.content,
                l.video_url,
                l.duration_minutes,
                l.order_index,
                m.title AS module_title
            FROM lessons l
            INNER JOIN modules m ON m.id = l.module_id
            ORDER BY m.order_index, l.order_index, l.id;
        """
        return pd.read_sql_query(query, conn)
    finally:
        conn.close()


def get_questions(lesson_id: int) -> pd.DataFrame:
    conn = get_connection()
    try:
        query = """
            SELECT *
            FROM questions
            WHERE lesson_id = %s
            ORDER BY order_index, id;
        """
        return pd.read_sql_query(query, conn, params=(lesson_id,))
    finally:
        conn.close()


def get_user_progress(user_id: int) -> pd.DataFrame:
    conn = get_connection()
    try:
        query = """
            SELECT
                l.title AS leccion,
                m.title AS modulo,
                up.best_score AS mejor_puntaje,
                up.is_completed AS completado,
                up.completed_at AS fecha_finalizacion
            FROM user_progress up
            INNER JOIN lessons l ON l.id = up.lesson_id
            INNER JOIN modules m ON m.id = l.module_id
            WHERE up.user_id = %s
            ORDER BY m.order_index, l.order_index;
        """
        return pd.read_sql_query(query, conn, params=(user_id,))
    finally:
        conn.close()


def register_student(registration_code: str, first_name: str, last_name: str):
    registration_code = registration_code.strip()
    first_name = first_name.strip()
    last_name = last_name.strip()

    if not registration_code or not first_name or not last_name:
        return False, "Completa el número de registro, nombre y apellido."

    if len(registration_code) < 4:
        return False, "El número de registro debe tener al menos 4 caracteres."

    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute(
            "SELECT id FROM users WHERE registration_code = %s;",
            (registration_code,)
        )

        if cur.fetchone():
            return False, "Este número de registro ya existe. Inicia sesión."

        cur.execute(
            """
            INSERT INTO users (
                registration_code,
                first_name,
                last_name,
                password_hash,
                role,
                must_change_password
            )
            VALUES (%s, %s, %s, %s, 'student', FALSE);
            """,
            (
                registration_code,
                first_name,
                last_name,
                hash_password(registration_code)
            )
        )

        conn.commit()
        return True, (
            "Cuenta creada correctamente. Tu usuario y contraseña "
            "son tu número de registro."
        )
    except Exception as error:
        if conn:
            conn.rollback()
        return False, f"Error al registrar: {error}"
    finally:
        if conn:
            conn.close()


def login_student(registration_code: str, password: str):
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT
                id,
                registration_code,
                first_name,
                last_name,
                password_hash,
                role
            FROM users
            WHERE registration_code = %s;
            """,
            (registration_code.strip(),)
        )

        user = cur.fetchone()

        if not user:
            return None, "No existe un usuario con ese número de registro."

        if not verify_password(password, user[4]):
            return None, "Número de registro o contraseña incorrectos."

        return {
            "id": user[0],
            "registration_code": user[1],
            "first_name": user[2],
            "last_name": user[3],
            "role": user[5]
        }, None
    except Exception as error:
        return None, f"Error al iniciar sesión: {error}"
    finally:
        if conn:
            conn.close()


def image_to_base64(image_data) -> str:
    image = Image.fromarray(image_data.astype("uint8"))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def uploaded_image_to_base64(uploaded_file) -> str:
    return base64.b64encode(uploaded_file.getvalue()).decode("utf-8")


def save_quiz_attempt(user_id: int, lesson_id: int, questions: pd.DataFrame, student_answers: dict):
    total_points = 0
    obtained_points = 0
    drawing_was_sent = False

    for _, question in questions.iterrows():
        question_id = int(question["id"])
        question_type = question["question_type"]
        points = int(question["points"])
        answer = student_answers.get(question_id, {})

        if question_type == "multiple_choice":
            total_points += points
            if answer.get("selected_option") == question["correct_option"]:
                obtained_points += points

        elif question_type == "drawing":
            total_points += points
            if answer.get("image_base64"):
                obtained_points += points
                drawing_was_sent = True

    score = round((obtained_points / total_points) * 100, 2) if total_points else 0
    passed = score >= 70

    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO quiz_attempts (user_id, lesson_id, score, passed)
            VALUES (%s, %s, %s, %s)
            RETURNING id;
            """,
            (user_id, lesson_id, score, passed)
        )
        attempt_id = cur.fetchone()[0]

        for _, question in questions.iterrows():
            question_id = int(question["id"])
            question_type = question["question_type"]
            points = int(question["points"])
            answer = student_answers.get(question_id, {})

            if question_type == "multiple_choice":
                selected_option = answer.get("selected_option")
                is_correct = selected_option == question["correct_option"]
                points_obtained = points if is_correct else 0

                cur.execute(
                    """
                    INSERT INTO question_answers (
                        attempt_id,
                        question_id,
                        selected_option,
                        text_answer,
                        is_correct,
                        points_obtained
                    )
                    VALUES (%s, %s, %s, NULL, %s, %s);
                    """,
                    (
                        attempt_id,
                        question_id,
                        selected_option,
                        is_correct,
                        points_obtained
                    )
                )

            elif question_type == "open_text":
                cur.execute(
                    """
                    INSERT INTO question_answers (
                        attempt_id,
                        question_id,
                        selected_option,
                        text_answer,
                        is_correct,
                        points_obtained
                    )
                    VALUES (%s, %s, NULL, %s, NULL, 0);
                    """,
                    (
                        attempt_id,
                        question_id,
                        answer.get("text_answer", "")
                    )
                )

            elif question_type == "drawing":
                image_base64 = answer.get("image_base64")

                if image_base64:
                    cur.execute(
                        """
                        INSERT INTO drawing_answers (
                            attempt_id,
                            question_id,
                            image_base64,
                            teacher_feedback,
                            reviewed,
                            points_obtained
                        )
                        VALUES (%s, %s, %s, %s, TRUE, %s);
                        """,
                        (
                            attempt_id,
                            question_id,
                            image_base64,
                            "Dibujo recibido y aceptado automáticamente.",
                            points
                        )
                    )

        cur.execute(
            """
            INSERT INTO user_progress (
                user_id,
                lesson_id,
                best_score,
                is_completed,
                completed_at,
                updated_at
            )
            VALUES (
                %s,
                %s,
                %s,
                %s,
                CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END,
                CURRENT_TIMESTAMP
            )
            ON CONFLICT (user_id, lesson_id)
            DO UPDATE SET
