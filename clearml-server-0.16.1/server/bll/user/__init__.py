from apierrors import errors
from apimodels.users import CreateRequest
from database.errors import translate_errors_context
from database.model.user import User


class UserBLL:
    @staticmethod
    def create(request: CreateRequest):
        user_id = request.id
        with translate_errors_context("creating user"):
            if user_id and User.objects(id=user_id).only("id"):
                raise errors.bad_request.UserIdExists(id=user_id)

            user = User(**request.to_struct())
            user.save(force_insert=True)

    @staticmethod
    def delete(user_id: str):
        with translate_errors_context("deleting user"):
            res = User.objects(id=user_id).delete()
            if not res:
                raise errors.bad_request.InvalidUserId(id=user_id)
